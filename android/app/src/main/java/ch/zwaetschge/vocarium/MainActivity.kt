package ch.zwaetschge.vocarium

import android.Manifest
import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.graphics.Color
import android.graphics.Bitmap
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.view.Gravity
import android.view.View
import android.webkit.*
import android.widget.*
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.graphics.Insets
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import org.json.JSONObject
import java.io.OutputStream
import java.util.UUID
import java.util.concurrent.Executors
import android.util.Base64

/** One retained WebView owns all four workflows and their audio transport. */
class MainActivity : AppCompatActivity() {
    private lateinit var webView: WebView
    private lateinit var container: FrameLayout
    private lateinit var loading: LinearLayout
    private lateinit var progress: ProgressBar
    private lateinit var problem: LinearLayout
    private lateinit var problemText: TextView
    private var fileChooser: ValueCallback<Array<Uri>>? = null
    private var playbackActive = false
    private var failedNavigation = false
    private var lastUrl = NavigationPolicy.HOME
    private var retryUrl = NavigationPolicy.HOME
    private var documentReady = false
    private var pendingBlob: String? = null
    @Volatile private var downloadId: String? = null
    private var downloadStream: OutputStream? = null
    private val downloadWorker = Executors.newSingleThreadExecutor()
    private val saveBlob = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val uri=result.data?.data
        val blob=pendingBlob
        pendingBlob=null
        if(result.resultCode!=RESULT_OK || uri==null || blob==null) {
            webView.evaluateJavascript("delete window.__vocariumPendingDownload;",null)
        }
        if(result.resultCode==RESULT_OK && uri!=null && blob!=null) {
            val token=UUID.randomUUID().toString()
            downloadId=token
            downloadWorker.execute {
                try {
                    downloadStream=contentResolver.openOutputStream(uri) ?: error("No output stream")
                    runOnUiThread { streamBlob(token) }
                } catch(_:Exception) {downloadId=null;runOnUiThread {toast("Die Datei konnte nicht gespeichert werden")}}
            }
        }
    }
    private val prefs by lazy { getSharedPreferences("workspace", MODE_PRIVATE) }

    private val pickFiles = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        fileChooser?.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data))
        fileChooser = null
    }
    private val askNotifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }
    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        lastUrl = prefs.getString("last_url", NavigationPolicy.HOME)?.takeIf { NavigationPolicy.isWorkspace(Uri.parse(it)) } ?: NavigationPolicy.HOME
        retryUrl = lastUrl
        container = FrameLayout(this).apply { setBackgroundColor(Color.rgb(17,20,31)) }
        webView = WebView(this).apply { setBackgroundColor(Color.rgb(17,20,31)) }
        container.addView(webView, FrameLayout.LayoutParams(-1,-1))
        createStatusViews()
        setContentView(container)
        val handled = WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout() or WindowInsetsCompat.Type.ime()
        ViewCompat.setOnApplyWindowInsetsListener(container) { view, insets ->
            val safe = insets.getInsets(handled)
            view.setPadding(safe.left,safe.top,safe.right,safe.bottom)
            WindowInsetsCompat.Builder(insets).setInsets(handled,Insets.NONE)
                .setInsetsIgnoringVisibility(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout(),Insets.NONE).build()
        }
        ViewCompat.requestApplyInsets(container)
        with(webView.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            loadWithOverviewMode = true
            useWideViewPort = true
            textZoom = (resources.configuration.fontScale * 100).toInt()
            userAgentString = "$userAgentString VocariumApp/${BuildConfig.VERSION_NAME}"
            allowFileAccess = false
            allowContentAccess = true // Storage Access Framework uploads
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        }
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView,true)
        configureBridge()
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                if (NavigationPolicy.isInternal(request.url)) return false
                if (request.isForMainFrame) openExternal(request.url)
                return true
            }
            override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
                if(downloadId!=null) {
                    downloadId=null
                    downloadWorker.execute {runCatching {downloadStream?.close()};downloadStream=null}
                    toast("Speichern durch Seitenwechsel unterbrochen")
                }
                documentReady = false
                failedNavigation = false
                problem.visibility = View.GONE
                (problem.parent as View).visibility = View.GONE
                loading.visibility = View.VISIBLE
                if (NavigationPolicy.isInternal(Uri.parse(url))) retryUrl = url
            }
            override fun onPageFinished(view: WebView, url: String) {
                loading.visibility = View.GONE
                documentReady = !failedNavigation && NavigationPolicy.isWorkspace(Uri.parse(url))
                if (documentReady) {
                    remember(url)
                    if (!WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) view.evaluateJavascript(bridgeScript(),null)
                    CookieManager.getInstance().flush()
                }
            }
            override fun doUpdateVisitedHistory(view: WebView, url: String, isReload: Boolean) { remember(url) }
            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) showProblem("Vocarium ist gerade nicht erreichbar. Prüfe deine Verbindung und versuche es erneut. Deine gespeicherten Projekte bleiben erhalten.")
            }
            override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
                if (request.isForMainFrame && response.statusCode >= 500) showProblem("Der Server antwortet gerade nicht. Du kannst diese Arbeitsstelle erneut öffnen.")
            }
        }
        webView.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView, value: Int) { progress.progress = value }
            override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                fileChooser?.onReceiveValue(null)
                fileChooser = callback
                try { pickFiles.launch(params.createIntent()) }
                catch (_: ActivityNotFoundException) { fileChooser?.onReceiveValue(null); fileChooser=null; toast("Keine Dateiauswahl verfügbar") }
                return true
            }
        }
        webView.setDownloadListener { url, agent, disposition, mime, _ -> download(url,agent,disposition,mime) }
        onBackPressedDispatcher.addCallback(this,object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (problem.visibility == View.VISIBLE) { navigate(lastUrl); return }
                if (documentReady) webView.evaluateJavascript("Boolean(window.__vocariumBack && window.__vocariumBack())") { handled ->
                    if (handled != "true") backInWebView()
                } else backInWebView()
            }
        })
        val link=intent?.data?.takeIf { intent.action == Intent.ACTION_VIEW && NavigationPolicy.isWorkspace(it) }
        if (link != null) webView.loadUrl(link.toString())
        else if (savedInstanceState == null || webView.restoreState(savedInstanceState) == null) webView.loadUrl(lastUrl)
        handleMediaIntent(intent)
    }

    /** Origin-restricted bridge: login pages and embedded third-party frames have no native privileges. */
    private fun configureBridge() {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) return
        WebViewCompat.addWebMessageListener(webView,"VocariumNative",setOf(NavigationPolicy.ORIGIN)) { _,message,origin,mainFrame,reply ->
            if (mainFrame && NavigationPolicy.isAppOrigin(origin)) runCatching {
                val data=JSONObject(message.data ?: "{}")
                when(data.optString("type")) {
                    "playback" -> updatePlayback(data.optBoolean("playing"),data.optString("title").take(300),data.optString("subtitle").take(300))
                    "location" -> remember(data.optString("url"))
                    "downloadChunk", "downloadEnd", "downloadError" -> {
                        if(data.optString("id")==downloadId && downloadId!=null) {
                            downloadWorker.execute {
                                var result="ack"
                                try {
                                    when(data.optString("type")) {
                                        "downloadChunk" -> {
                                            val encoded=data.optString("data")
                                            require(encoded.length<=100000)
                                            downloadStream?.write(Base64.decode(encoded,Base64.DEFAULT)) ?: error("Download closed")
                                        }
                                        "downloadEnd" -> {downloadStream?.close();downloadStream=null;downloadId=null;runOnUiThread {toast("Datei gespeichert")}}
                                        else -> error("Download interrupted")
                                    }
                                } catch(_:Exception) {
                                    runCatching {downloadStream?.close()};downloadStream=null;downloadId=null;result="error"
                                    runOnUiThread {toast("Speichern unterbrochen. Bitte die Datei erneut herunterladen.")}
                                }
                                webView.post {if(!isDestroyed && !isFinishing) runCatching {reply.postMessage(result)}}
                            }
                        }
                    }
                }
            }
        }
        if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT))
            WebViewCompat.addDocumentStartJavaScript(webView,bridgeScript(),setOf(NavigationPolicy.ORIGIN))
    }
    private fun bridgeScript(): String = """
        (function(){if(window.top!==window||!window.VocariumNative||window.VocariumAndroid)return;
        window.VocariumAndroid={isApp:()=>true,version:()=>${JSONObject.quote(BuildConfig.VERSION_NAME)},
        setPlayback:(playing,title,subtitle)=>VocariumNative.postMessage(JSON.stringify({type:'playback',playing,title,subtitle}))};
        const report=()=>VocariumNative.postMessage(JSON.stringify({type:'location',url:location.href}));
        for(const key of ['pushState','replaceState']){const original=history[key];history[key]=function(){const r=original.apply(this,arguments);report();return r;};}
        window.addEventListener('popstate',report);})();
    """.trimIndent()

    private fun createStatusViews() {
        loading=LinearLayout(this).apply {
            orientation=LinearLayout.VERTICAL; gravity=Gravity.CENTER; setPadding(dp(28),dp(32),dp(28),dp(32)); setBackgroundColor(Color.rgb(17,20,31))
            addView(TextView(this@MainActivity).apply {text="Vocarium";textSize=30f;setTextColor(Color.WHITE);gravity=Gravity.CENTER})
            addView(TextView(this@MainActivity).apply {text="Dein Arbeitsbereich wird geöffnet …";textSize=15f;setTextColor(Color.LTGRAY);gravity=Gravity.CENTER;setPadding(0,dp(16),0,dp(24))})
        }
        progress=ProgressBar(this,null,android.R.attr.progressBarStyleHorizontal)
        loading.addView(progress,LinearLayout.LayoutParams(dp(240),dp(4)))
        container.addView(loading,FrameLayout.LayoutParams(-1,-1))
        problem=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL;gravity=Gravity.CENTER;setPadding(dp(24),dp(24),dp(24),dp(24));setBackgroundColor(Color.rgb(17,20,31));visibility=View.GONE}
        problem.addView(TextView(this).apply {text="Verbindung unterbrochen";textSize=24f;setTextColor(Color.WHITE);gravity=Gravity.CENTER})
        problemText=TextView(this).apply {textSize=16f;setTextColor(Color.LTGRAY);gravity=Gravity.CENTER;setPadding(0,dp(16),0,dp(20))}
        problem.addView(problemText,LinearLayout.LayoutParams(-1,-2))
        problem.addView(Button(this).apply {text="Erneut versuchen";minHeight=dp(48);setOnClickListener {navigate(retryUrl)}})
        problem.addView(TextView(this).apply {text="Oder einen Arbeitsbereich öffnen";textSize=14f;setTextColor(Color.LTGRAY);setPadding(0,dp(20),0,dp(8))})
        NavigationPolicy.modes.chunked(2).forEach { row ->
            val line=LinearLayout(this)
            row.forEach { (label,path) -> line.addView(Button(this).apply {text=label;isAllCaps=false;minHeight=dp(48);setOnClickListener {navigate(NavigationPolicy.ORIGIN+path)}},LinearLayout.LayoutParams(0,-2,1f)) }
            problem.addView(line,LinearLayout.LayoutParams(-1,-2))
        }
        val scroller=ScrollView(this).apply {isFillViewport=true;addView(problem)}
        // Hide the complete overlay, not just its contents, when the page succeeds.
        container.addView(scroller,FrameLayout.LayoutParams(-1,-1))
        scroller.visibility=View.GONE
    }
    private fun showProblem(message:String) {
        failedNavigation=true;documentReady=false;loading.visibility=View.GONE
        problemText.text=message;problem.visibility=View.VISIBLE;(problem.parent as View).visibility=View.VISIBLE
        problem.announceForAccessibility("Verbindung unterbrochen. $message")
    }
    private fun remember(url:String) {
        if (NavigationPolicy.isWorkspace(Uri.parse(url))) {lastUrl=url;prefs.edit().putString("last_url",url).apply()}
    }
    private fun navigate(url:String) {
        (problem.parent as View).visibility=View.GONE;problem.visibility=View.GONE
        val uri=Uri.parse(url)
        if (documentReady && NavigationPolicy.isWorkspace(uri)) {
            val route=JSONObject.quote(NavigationPolicy.route(uri))
            webView.evaluateJavascript("(function(){if(!window.__vocariumNavigate)return false;window.__vocariumNavigate($route);return true;})()") { handled -> if(handled!="true") webView.loadUrl(url) }
        } else webView.loadUrl(url)
    }
    private fun backInWebView() {if(webView.canGoBack()) webView.goBack() else moveTaskToBack(true)}
    private fun openExternal(uri:Uri) {
        if(uri.scheme !in listOf("https","http","mailto","tel")) {toast("Dieser Link kann hier nicht geöffnet werden");return}
        try {startActivity(Intent(Intent.ACTION_VIEW,uri))} catch(_:ActivityNotFoundException){toast("Keine passende App für diesen Link")}
    }
    private fun download(url:String,agent:String,disposition:String,mime:String) {
        val uri=Uri.parse(url)
        if(url.startsWith("blob:${NavigationPolicy.ORIGIN}/")) {
            if(pendingBlob!=null || downloadId!=null) {toast("Bitte den laufenden Speichervorgang abschließen");return}
            if(!documentReady || !WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {toast("Bitte Android System WebView aktualisieren, um diese Datei zu speichern");return}
            pendingBlob=url
            // Keep the Blob alive before the page revokes its temporary URL.
            webView.evaluateJavascript("window.__vocariumPendingDownload=fetch(${JSONObject.quote(url)}).then(r=>r.blob());window.__vocariumPendingDownload.catch(()=>{});",null)
            val name=if(mime.startsWith("audio/")) "vocarium-aufnahme.wav" else if(mime.contains("json")) "vocarium-export.json" else "vocarium-export.txt"
            try {saveBlob.launch(Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType(mime.ifBlank {"application/octet-stream"}).putExtra(Intent.EXTRA_TITLE,name))}
            catch(_:ActivityNotFoundException) {pendingBlob=null;toast("Keine Dateiauswahl verfügbar")}
            return
        }
        if(!NavigationPolicy.isAppOrigin(uri)) {toast("Dieser Download benötigt einen direkten Vocarium-Dateilink");return}
        try {
            val filename=URLUtil.guessFileName(url,disposition,mime).map { if (it == '/' || it == '\\' || it.code == 0) '_' else it }.joinToString("")
            val request=DownloadManager.Request(uri).addRequestHeader("Cookie",CookieManager.getInstance().getCookie(url)?:"")
                .addRequestHeader("User-Agent",agent).setMimeType(mime).setTitle(filename)
                .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS,filename)
            (getSystemService(DOWNLOAD_SERVICE) as DownloadManager).enqueue(request);toast("Download gestartet: $filename")
        } catch(_:Exception){toast("Download konnte nicht gestartet werden. Bitte erneut versuchen.")}
    }
    /** Save browser-generated WAV/text in acknowledged small chunks, without one giant base64 allocation. */
    private fun streamBlob(token:String) {
        val js="""
            (async function(){
              const id=${JSONObject.quote(token)};
              const send=data=>new Promise((resolve,reject)=>{
                const timer=setTimeout(()=>reject(new Error('Save timeout')),30000);
                VocariumNative.onmessage=event=>{clearTimeout(timer);event.data==='ack'?resolve():reject(new Error('Save failed'));};
                VocariumNative.postMessage(JSON.stringify({...data,id}));
              });
              try {
                const blob=await window.__vocariumPendingDownload;
                const reader=blob.stream().getReader();
                while(true){const {done,value}=await reader.read();if(done)break;
                  for(let start=0;start<value.length;start+=16384){
                    const data=btoa(String.fromCharCode(...value.subarray(start,start+16384)));
                    await send({type:'downloadChunk',data});
                  }
                }
                await send({type:'downloadEnd'});
              } catch(error){VocariumNative.postMessage(JSON.stringify({type:'downloadError',id}));}
              finally {VocariumNative.onmessage=null;delete window.__vocariumPendingDownload;}
            })();
        """.trimIndent()
        webView.evaluateJavascript(js,null)
    }

    private fun toast(text:String) {Toast.makeText(this,text,Toast.LENGTH_LONG).show()}
    override fun onNewIntent(intent:Intent) {
        super.onNewIntent(intent);setIntent(intent)
        intent.data?.takeIf {intent.action==Intent.ACTION_VIEW && NavigationPolicy.isWorkspace(it)}?.let {navigate(it.toString())}
        handleMediaIntent(intent)
    }
    private fun handleMediaIntent(intent:Intent?) {
        if(intent?.action!=ACTION_MEDIA)return
        val action=intent.getStringExtra(EXTRA_MEDIA_ACTION)?:return
        if(documentReady) webView.evaluateJavascript("window.__vocariumMediaAction && window.__vocariumMediaAction(${JSONObject.quote(action)});",null)
    }
    private fun updatePlayback(playing:Boolean,title:String,subtitle:String) {
        val service=Intent(this,PlaybackService::class.java)
        if(!playing && title.isEmpty()) {if(playbackActive)startService(service.setAction(PlaybackService.ACTION_STOP));playbackActive=false;return}
        if(playing && !prefs.getBoolean("notifications_requested",false) && Build.VERSION.SDK_INT>=33 && !isFinishing && lifecycle.currentState.isAtLeast(androidx.lifecycle.Lifecycle.State.RESUMED)) {
            prefs.edit().putBoolean("notifications_requested",true).apply()
            if(ContextCompat.checkSelfPermission(this,Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED) askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        service.putExtra(PlaybackService.EXTRA_PLAYING,playing).putExtra(PlaybackService.EXTRA_TITLE,title).putExtra(PlaybackService.EXTRA_SUBTITLE,subtitle)
        if(Build.VERSION.SDK_INT>=26)startForegroundService(service) else startService(service)
        playbackActive=true
    }
    override fun onSaveInstanceState(state:Bundle) {super.onSaveInstanceState(state);webView.saveState(state)}
    override fun onConfigurationChanged(config:Configuration) {
        super.onConfigurationChanged(config)
        webView.settings.textZoom=(config.fontScale*100).toInt()
        ViewCompat.requestApplyInsets(container);webView.requestLayout()
    }
    override fun onDestroy() {
        fileChooser?.onReceiveValue(null);fileChooser=null
        CookieManager.getInstance().flush()
        if(playbackActive)startService(Intent(this,PlaybackService::class.java).setAction(PlaybackService.ACTION_STOP))
        downloadWorker.execute {runCatching {downloadStream?.close()};downloadStream=null}
        downloadWorker.shutdown()
        webView.destroy();super.onDestroy()
    }
    companion object {
        const val ACTION_MEDIA="ch.zwaetschge.vocarium.MEDIA_ACTION"
        const val EXTRA_MEDIA_ACTION="media_action"
    }
}
