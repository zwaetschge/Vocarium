package ch.zwaetschge.vocarium

import android.annotation.SuppressLint
import android.app.AlertDialog
import android.graphics.Color
import android.graphics.BitmapFactory
import android.text.Editable
import android.text.TextWatcher
import android.text.TextUtils
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.webkit.WebView
import android.webkit.WebViewClient
import android.webkit.WebResourceRequest
import android.widget.*
import androidx.appcompat.app.AppCompatActivity
import androidx.activity.OnBackPressedCallback
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors
import java.io.File

/** All workspaces are native. WebView is confined to external sign-in. */
class NativeActivity : AppCompatActivity() {
    private val api = NativeApi()
    private val worker = Executors.newFixedThreadPool(3)
    private val coverWorker = Executors.newFixedThreadPool(2)
    private lateinit var shell: LinearLayout
    private lateinit var root: FrameLayout
    private var loginOverlay: View? = null
    private lateinit var content: LinearLayout
    private lateinit var title: TextView
    private lateinit var headerBack: FrameLayout
    private lateinit var headerActions: LinearLayout
    private lateinit var miniCover: NativeCover
    private lateinit var miniProgress: ProgressBar
    private var miniTrack: Track? = null
    private var contentWide = false
    private val areaIcons = listOf("books","podcasts","drama","studio")
    @Volatile private var generation = 0
    private var area = 0
    private var navigationMode = 0
    private lateinit var navigation: LinearLayout
    private lateinit var miniPlayer: LinearLayout
    private lateinit var miniTitle: TextView
    private lateinit var miniToggle: FrameLayout
    private var lastMiniPlaying: Boolean? = null
    private var playerVisible = false
    private var destination: (() -> Unit)? = null
    private var query = ""
    private var bookFilter = 0
    private var collectionFilter = ""
    private var hsFilter = 0
    private var filtersReload:(()->Unit)?=null
    private var lastShellWidth=0
    private val coverCache = android.util.LruCache<String, android.graphics.Bitmap>(24)
    private var pendingPick:((android.net.Uri)->Unit)?=null
    private val notificationPermission=registerForActivityResult(androidx.activity.result.contract.ActivityResultContracts.RequestPermission()) {}
    private val picker=registerForActivityResult(androidx.activity.result.contract.ActivityResultContracts.OpenDocument()) {uri->val done=pendingPick;pendingPick=null;if(uri!=null)done?.invoke(uri)}

    private val pages = java.util.ArrayDeque<() -> Unit>()
    private val names = listOf("Hörbücher", "Podcasts", "Hörspiele", "Sprachstudio")
    private val textColor = NativeDesign.ink
    private val surfaceColor = NativeDesign.canvas
    private val prefs by lazy { getSharedPreferences("native-workspaces", MODE_PRIVATE) }
    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        shell = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setBackgroundColor(surfaceColor) }
        root = FrameLayout(this).apply { setBackgroundColor(surfaceColor) }
        ViewCompat.setOnApplyWindowInsetsListener(root) { view, insets ->
            val padding = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            view.setPadding(padding.left,padding.top,padding.right,padding.bottom); insets
        }
        val body=LinearLayout(this).apply {orientation=LinearLayout.HORIZONTAL}
        val mainColumn=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL}
        body.addView(mainColumn,LinearLayout.LayoutParams(0,-1,1f))
        shell.addView(body,LinearLayout.LayoutParams(-1,-1))
        val header = LinearLayout(this).apply { gravity = Gravity.CENTER_VERTICAL; setPadding(dp(8),dp(8),dp(8),dp(6)) }
        headerBack=iconButton("back","Zurück") { goBack() }.apply { visibility=View.GONE }
        header.addView(headerBack)
        title = NativeDesign.text(this,"Vocarium",19,bold=true).apply { maxLines=1; ellipsize=TextUtils.TruncateAt.END; setPadding(dp(12),0,dp(8),0) }
        header.addView(title,LinearLayout.LayoutParams(0,dp(48),1f).apply { title.gravity=Gravity.CENTER_VERTICAL })
        headerActions=LinearLayout(this)
        header.addView(headerActions)
        header.addView(iconButton("account","Konto und Einstellungen") { account() })
        mainColumn.addView(header)
        val scroll = ScrollView(this).apply { isFillViewport = true; clipToPadding=false }
        content = LinearLayout(this).apply { orientation=LinearLayout.VERTICAL; setPadding(dp(24),dp(8),dp(24),dp(28)) }
        scroll.addView(content,FrameLayout.LayoutParams(-1,-2,Gravity.CENTER_HORIZONTAL))
        val swipe=androidx.swiperefreshlayout.widget.SwipeRefreshLayout(this).apply {setColorSchemeColors(NativeDesign.accent);setProgressBackgroundColorSchemeColor(NativeDesign.panel);addView(scroll);setOnRefreshListener {isRefreshing=false;if(!playerVisible)destination?.invoke()}}
        mainColumn.addView(swipe,LinearLayout.LayoutParams(-1,0,1f))
        if(android.os.Build.VERSION.SDK_INT>=33 && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)!=android.content.pm.PackageManager.PERMISSION_GRANTED && !prefs.getBoolean("asked-notifications",false)){prefs.edit().putBoolean("asked-notifications",true).apply();notificationPermission.launch(android.Manifest.permission.POST_NOTIFICATIONS)}
        miniPlayer=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL;background=NativeDesign.shape(this@NativeActivity,NativeDesign.accentPanel,18,true);visibility=View.GONE;clipToOutline=true}
        val miniRow=LinearLayout(this).apply {gravity=Gravity.CENTER_VERTICAL;setPadding(dp(8),dp(6),dp(8),dp(4))}
        miniCover=NativeCover(this,"","mini");miniRow.addView(miniCover,LinearLayout.LayoutParams(dp(36),dp(48)))
        miniTitle=NativeDesign.text(this,"",14,bold=true).apply {maxLines=1;ellipsize=TextUtils.TruncateAt.END;setPadding(dp(10),0,dp(8),0);setOnClickListener {playerScreen()}}
        miniRow.addView(miniTitle,LinearLayout.LayoutParams(0,-2,1f))
        miniToggle=iconButton("play","Wiedergabe fortsetzen") {root.performHapticFeedback(android.view.HapticFeedbackConstants.CONTEXT_CLICK);NativePlaybackService.current?.let {if(it.playing)it.pause() else it.resume()}}
        miniRow.addView(miniToggle)
        miniRow.addView(iconButton("next","Player öffnen") {playerScreen()})
        miniPlayer.addView(miniRow)
        miniProgress=ProgressBar(this,null,android.R.attr.progressBarStyleHorizontal).apply {max=1000;progressTintList=android.content.res.ColorStateList.valueOf(NativeDesign.accent);progressBackgroundTintList=android.content.res.ColorStateList.valueOf(NativeDesign.border)}
        miniPlayer.addView(miniProgress,LinearLayout.LayoutParams(-1,dp(3)))
        mainColumn.addView(miniPlayer,LinearLayout.LayoutParams(-1,-2).apply {setMargins(dp(16),dp(6),dp(16),dp(8))})
        navigation = LinearLayout(this).apply { setPadding(dp(8),dp(5),dp(8),dp(9));setBackgroundColor(surfaceColor) }
        mainColumn.addView(navigation)
        val tick=object:Runnable {override fun run(){
            if(isDestroyed)return
            val service=NativePlaybackService.current
            miniPlayer.visibility=if(service!=null && !playerVisible)View.VISIBLE else View.GONE
            miniTitle.text=service?.error ?: service?.title.orEmpty()
            val current=service?.track
            if(current!==miniTrack){miniTrack=current;miniCover.bookTitle=current?.title.orEmpty();miniCover.bitmap=null;if(current!=null && current.bookId.isNotBlank())loadCover(miniCover,current.bookId)}
            if(miniCover.bitmap==null && current!=null && current.bookId.isNotBlank())coverCache.get(current.bookId)?.let {miniCover.bitmap=it}
            val total=service?.duration ?: 0L;miniProgress.progress=if(total>0)((service?.position ?: 0L)*1000/total).toInt() else 0
            val playing=service?.playing==true
            if(lastMiniPlaying!=playing){lastMiniPlaying=playing;miniToggle.removeAllViews();miniToggle.addView(NativeIcon(this@NativeActivity,if(playing)"pause" else "play",NativeDesign.ink),FrameLayout.LayoutParams(-1,-1));miniToggle.contentDescription=if(playing)"Wiedergabe pausieren" else "Wiedergabe fortsetzen"}
            miniPlayer.postDelayed(this,500)
        }};miniPlayer.post(tick)
        shell.addOnLayoutChangeListener {_,left,_,right,_,_,_,_,_->
            val width=(right-left)/resources.displayMetrics.density
            val next=if(width>=1024)2 else if(width>=720)1 else 0
            val widthPx=right-left
            if(widthPx>0 && lastShellWidth!=0 && widthPx!=lastShellWidth){shell.post {applyContentWidth()};filtersReload?.let {reload->shell.post {reload()}}}
            lastShellWidth=widthPx
            if(next!=navigationMode){
                navigationMode=next
                (navigation.parent as? android.view.ViewGroup)?.removeView(navigation)
                navigation.orientation=if(next==0)LinearLayout.HORIZONTAL else LinearLayout.VERTICAL
                if(next==0)mainColumn.addView(navigation,LinearLayout.LayoutParams(-1,-2))
                else body.addView(navigation,0,LinearLayout.LayoutParams(dp(if(next==2)208 else 88),-1))
                renderNavigation()
            }
        }
        root.addView(shell, FrameLayout.LayoutParams(-1, -1))
        setContentView(root)
        onBackPressedDispatcher.addCallback(this,object:OnBackPressedCallback(true) {
            override fun handleOnBackPressed() { goBack() }
        })
        area=prefs.getInt("area",0).coerceIn(0,3)
        openLink(intent?.data)
    }
    override fun onNewIntent(intent: android.content.Intent) { super.onNewIntent(intent); setIntent(intent); openLink(intent.data) }
    private fun openLink(uri: android.net.Uri?) {
        if(uri!=null && NavigationPolicy.isAppOrigin(uri)) {
            val parts=uri.pathSegments
            area=when(parts.firstOrNull()) {"podcast"->1;"hoerspiele"->2;"voices","clone","transcribe"->3;else->0}
            if(parts.size==2 && parts[0] in listOf("audiobooks","hoerspiele")) {
                pages.clear();pages.addLast {home()};detail(area,JSONObject().put("id",android.net.Uri.encode(parts[1])),names[area]);return
            }
        }
        home()
    }
    private fun label(text: String,size: Int=16) = NativeDesign.text(this,text,size,bold=size>=22).apply {setPadding(0,dp(8),0,dp(8));setLineSpacing(dp(4).toFloat(),1f)}
    private fun button(text:String,action:()->Unit) = NativeDesign.text(this,text,15,bold=true).apply {
        minHeight=dp(52);gravity=Gravity.CENTER_VERTICAL;setPadding(dp(18),dp(13),dp(18),dp(13))
        background=NativeDesign.touch(this@NativeActivity);isClickable=true;isFocusable=true;setOnClickListener {action()}
        accessibilityDelegate=object:View.AccessibilityDelegate(){override fun onInitializeAccessibilityNodeInfo(host:View,info:android.view.accessibility.AccessibilityNodeInfo){super.onInitializeAccessibilityNodeInfo(host,info);info.className="android.widget.Button"}}
    }
    private fun iconButton(icon:String,description:String,action:()->Unit) = FrameLayout(this).apply {
        layoutParams=LinearLayout.LayoutParams(dp(48),dp(48));background=NativeDesign.touch(this@NativeActivity,Color.TRANSPARENT,24)
        contentDescription=description;isClickable=true;isFocusable=true;setOnClickListener{action()}
        addView(NativeIcon(this@NativeActivity,icon,NativeDesign.ink),FrameLayout.LayoutParams(-1,-1))
    }
    private fun renderNavigation() {
        navigation.removeAllViews()
        val icons=listOf("books","podcasts","drama","studio")
        listOf("Bücher","Podcasts","Hörspiele","Studio").forEachIndexed { index,name ->
            val selected=index==area
            val cell=LinearLayout(this).apply {orientation=if(navigationMode==2)LinearLayout.HORIZONTAL else LinearLayout.VERTICAL;gravity=if(navigationMode==2)Gravity.CENTER_VERTICAL else Gravity.CENTER;setPadding(dp(2),dp(4),dp(2),dp(4));background=NativeDesign.touch(this@NativeActivity,if(selected)NativeDesign.accentPanel else Color.TRANSPARENT,16);isSelected=selected;isClickable=true;isFocusable=true;contentDescription=name;importantForAccessibility=View.IMPORTANT_FOR_ACCESSIBILITY_YES;setOnClickListener{area=index;query="";bookFilter=0;hsFilter=0;collectionFilter="";prefs.edit().putInt("area",area).apply();pages.clear();home()}}
            cell.addView(NativeIcon(this,icons[index],if(selected)NativeDesign.accent else NativeDesign.muted),LinearLayout.LayoutParams(dp(42),dp(38)))
            cell.addView(NativeDesign.text(this,name,if(navigationMode==2)14 else 11,if(selected)NativeDesign.ink else NativeDesign.muted,selected).apply {gravity=Gravity.CENTER;maxLines=1},LinearLayout.LayoutParams(if(navigationMode==2)-2 else -1,-2))
            navigation.addView(cell,(if(navigationMode==0)LinearLayout.LayoutParams(0,-2,1f) else LinearLayout.LayoutParams(-1,dp(76))).apply {setMargins(dp(3),0,dp(3),dp(if(navigationMode==0)0 else 6))})
        }
    }
    private fun goBack() { if(loginOverlay!=null) closeLogin() else if(pages.isNotEmpty()) pages.removeLast().invoke() else if(area!=0) {area=0;home()} else finish() }
    private fun reset(name:String,back:Boolean=false,wide:Boolean=false) { window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON); playerVisible=false; generation++; filtersReload=null; title.text=name; headerBack.visibility=if(back)View.VISIBLE else View.GONE; headerActions.removeAllViews(); contentWide=wide; applyContentWidth(); content.removeAllViews();(content.parent as? ScrollView)?.scrollTo(0,0);renderNavigation() }
    private fun headerAction(icon:String,description:String,block:()->Unit) { headerActions.addView(iconButton(icon,description,block)) }
    /** Reading and editing screens stay at a comfortable measure; the shelf may use the whole width. */
    private fun applyContentWidth() {
        val scroll=content.parent as? ScrollView ?: return
        val available=if(scroll.width>0)scroll.width else resources.displayMetrics.widthPixels
        val target=if(contentWide) -1 else minOf(available,dp(760))
        val params=(content.layoutParams as? FrameLayout.LayoutParams) ?: FrameLayout.LayoutParams(-1,-2)
        if(params.width!=target || params.gravity!=Gravity.CENTER_HORIZONTAL){params.width=target;params.gravity=Gravity.CENTER_HORIZONTAL;content.layoutParams=params}
    }
    private fun skeleton():View = LinearLayout(this).apply {
        orientation=LinearLayout.VERTICAL;setPadding(0,dp(8),0,dp(8))
        for(h in listOf(72,72,72))addView(View(this@NativeActivity).apply {background=NativeDesign.shape(this@NativeActivity,NativeDesign.panel,14)},LinearLayout.LayoutParams(-1,dp(h)).apply {bottomMargin=dp(10)})
        android.animation.ObjectAnimator.ofFloat(this,"alpha",1f,0.4f).apply {duration=700;repeatMode=android.animation.ValueAnimator.REVERSE;repeatCount=android.animation.ValueAnimator.INFINITE}.start()
    }
    private fun action(text:String,block:()->Unit) {content.addView(button(text,block),LinearLayout.LayoutParams(-1,-2).apply {bottomMargin=dp(8)})}
    private fun caption(text:String) = NativeDesign.text(this,text,13,NativeDesign.muted).apply {setPadding(0,dp(4),0,dp(8))}
    private fun loadCover(view:NativeCover,id:String,path:String="/api/audiobooks/${android.net.Uri.encode(id)}/cover") {
        coverCache.get(id)?.let {view.bitmap=it;return}
        val revision=generation
        coverWorker.execute {
            if(revision!=generation)return@execute
            coverCache.get(id)?.let {bitmap->runOnUiThread {if(!isDestroyed && revision==generation)view.bitmap=bitmap};return@execute}
            val bitmap=runCatching {
                val connection=java.net.URL(NavigationPolicy.ORIGIN+path).openConnection() as java.net.HttpURLConnection
                try {connection.instanceFollowRedirects=false;connection.connectTimeout=10000;connection.readTimeout=15000
                    android.webkit.CookieManager.getInstance().getCookie(NavigationPolicy.ORIGIN)?.let {connection.setRequestProperty("Cookie",it)}
                    if(connection.responseCode==200 && connection.contentType?.startsWith("image/")==true) {
                        val bytes=connection.inputStream.use {it.readBytesWithLimit(6*1024*1024)}
                        val bounds=BitmapFactory.Options().apply {inJustDecodeBounds=true};BitmapFactory.decodeByteArray(bytes,0,bytes.size,bounds)
                        val options=BitmapFactory.Options().apply {inSampleSize=maxOf(1,maxOf(bounds.outWidth,bounds.outHeight)/600)}
                        BitmapFactory.decodeByteArray(bytes,0,bytes.size,options)
                    } else null
                } finally {connection.disconnect()}
            }.getOrNull()
            if(bitmap!=null){coverCache.put(id,bitmap);runOnUiThread {if(!isDestroyed && revision==generation)view.bitmap=bitmap}}
        }
    }
    private fun java.io.InputStream.readBytesWithLimit(limit:Int):ByteArray {
        val buffer=java.io.ByteArrayOutputStream();val chunk=ByteArray(8192)
        while(true){val count=read(chunk);if(count<0)break;if(buffer.size()+count>limit)throw java.io.IOException("Die Datei ist zu groß.");buffer.write(chunk,0,count)}
        return buffer.toByteArray()
    }
    private fun jsonCacheFile(path:String)=File(File(filesDir,"jsoncache").apply {mkdirs()},java.security.MessageDigest.getInstance("SHA-1").digest(path.toByteArray()).joinToString("") {"%02x".format(it)}+".json")
    private fun storeJson(path:String,value:Any) {runCatching {jsonCacheFile(path).writeText(value.toString())}}
    private fun loadJson(path:String):Pair<Any,Long>? {val file=jsonCacheFile(path);if(!file.isFile)return null;return runCatching {org.json.JSONTokener(file.readText()).nextValue() to file.lastModified()}.getOrNull()}
    /** Network first; without a connection the last stored answer is used so the library and prefetched chapters keep working. */
    private fun requestCached(path:String):Any {
        val result=runCatching {api.request(path)}
        result.getOrNull()?.let {storeJson(path,it);return it}
        val error=result.exceptionOrNull()!!
        if(error is LoginRequired)throw error
        return loadJson(path)?.first ?: throw error
    }
    private fun offlineBanner(stamp:Long)=caption("Offline · gespeicherter Stand vom ${java.text.SimpleDateFormat("d. MMM, HH:mm",java.util.Locale.GERMAN).format(java.util.Date(stamp))}").apply {setTextColor(Color.rgb(255,204,150));setPadding(0,dp(4),0,dp(10))}
    private fun fetch(path:String,into:LinearLayout=content,cache:Boolean=false,render:(Any)->Unit) {
        val revision=generation
        val progress=skeleton();into.addView(progress)
        worker.execute {
            val result=runCatching {api.request(path)}
            runOnUiThread {
                if(isDestroyed || revision!=generation) return@runOnUiThread
                into.removeView(progress)
                result.onSuccess {value->if(cache)storeJson(path,value);render(value)}.onFailure {error ->
                    val cached=if(cache && error !is LoginRequired)loadJson(path) else null
                    if(cached!=null){into.addView(offlineBanner(cached.second));render(cached.first);return@onFailure}
                    into.addView(label(error.message ?: "Verbindung fehlgeschlagen."))
                    if(error is LoginRequired) into.addView(button("Anmelden") {login()},LinearLayout.LayoutParams(-1,-2).apply {bottomMargin=dp(8)})
                    else into.addView(button("Erneut versuchen") {destination?.invoke() ?: home()},LinearLayout.LayoutParams(-1,-2).apply {bottomMargin=dp(8)})
                }
            }
        }
    }
    private fun array(value:Any,key:String) = if(value is JSONArray) value else (value as JSONObject).optJSONArray(key) ?: JSONArray()
    private fun home() {
        if(area==3){studio();return}
        destination={home()}
        reset(names[area],wide=true)
        val currentArea=area
        val search=EditText(this).apply {hint=if(area==0)"Titel oder Autor suchen" else "Suchen";textSize=16f;setTextColor(textColor);setHintTextColor(NativeDesign.muted);setSingleLine(true);setPadding(dp(16),0,dp(12),0);background=NativeDesign.shape(this@NativeActivity,NativeDesign.panel,14);setText(query);contentDescription=hint;imeOptions=imeOptions or android.view.inputmethod.EditorInfo.IME_FLAG_NO_EXTRACT_UI or android.view.inputmethod.EditorInfo.IME_FLAG_NO_FULLSCREEN;visibility=if(query.isNotBlank())View.VISIBLE else View.GONE}
        content.addView(search,LinearLayout.LayoutParams(-1,dp(52)).apply {topMargin=dp(6);bottomMargin=dp(14)})
        headerAction("search","Suchen") {if(search.visibility==View.GONE){search.visibility=View.VISIBLE;search.requestFocus();getSystemService(android.view.inputmethod.InputMethodManager::class.java).showSoftInput(search,0)} else {search.visibility=View.GONE;getSystemService(android.view.inputmethod.InputMethodManager::class.java).hideSoftInputFromWindow(search.windowToken,0)}}
        if(currentArea==0)headerAction("sort","Sortierung") {val modes=listOf("recent","title","added");val current=prefs.getString("sort-books","recent") ?: "recent";AlertDialog.Builder(this).setTitle("Sortierung").setSingleChoiceItems(arrayOf("Zuletzt gehört","Titel","Zuletzt hinzugefügt"),modes.indexOf(current).coerceAtLeast(0)){dialog,i->prefs.edit().putString("sort-books",modes[i]).apply();dialog.dismiss();filtersReload?.invoke()}.setNegativeButton("Abbrechen",null).show()}
        if(currentArea==0){headerAction("plus","Buch importieren") {pick("application/pdf","application/epub+zip","text/plain","application/vnd.openxmlformats-officedocument.wordprocessingml.document") {uri->importBook(uri)}};headerAction("list","Warteschlange") {pages.addLast {area=0;home()};queueScreen()}}
        if(currentArea==1){headerAction("plus","Neue Episode") {podcastForm(null){created->pages.addLast {area=1;home()};detail(1,created,created.optString("topic").ifBlank {"Neue Episode"})}};headerAction("people","Moderatoren") {pages.addLast {area=1;home()};hostsScreen()}}
        val filters=LinearLayout(this).apply {gravity=Gravity.CENTER_VERTICAL}
        if(currentArea==0 || currentArea==2)content.addView(HorizontalScrollView(this).apply {isHorizontalScrollBarEnabled=false;addView(filters)},LinearLayout.LayoutParams(-1,-2).apply {bottomMargin=dp(18)})
        var collections=JSONArray()
        val homeRevision=generation
        if(currentArea==0)worker.execute {val loaded=runCatching {array(api.request("/api/audiobooks/collections"),"collections")}.getOrNull();if(loaded!=null && loaded.length()>0)runOnUiThread {if(!isDestroyed && generation==homeRevision){collections=loaded;filtersReload?.invoke()}}}
        val results=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL}
        content.addView(results)
        val endpoint=listOf("/api/audiobooks","/api/podcasts","/api/hoerspiele/projects","/api/voices")[area]
        val key=listOf("books","podcasts","projects","voices")[area]
        fetch(endpoint,cache=true) {data ->
            val all=array(data,key)
            fun render() {
                results.removeAllViews();filters.removeAllViews()
                if(currentArea==0) {
                    fun chip(name:String,selected:Boolean,click:()->Unit) {filters.addView(button(name){click()}.apply {textSize=12f;minHeight=dp(44);setPadding(dp(12),dp(10),dp(12),dp(10));background=NativeDesign.touch(this@NativeActivity,if(selected)NativeDesign.accentPanel else NativeDesign.panel,22);setTextColor(if(selected)NativeDesign.ink else NativeDesign.muted);isSelected=selected},LinearLayout.LayoutParams(-2,-2).apply {rightMargin=dp(6)})}
                    listOf("Alle Bücher","Begonnen","Beendet","Versteckt").forEachIndexed {index,name->chip(name,bookFilter==index && collectionFilter.isEmpty()){bookFilter=index;collectionFilter="";render()}}
                    for(i in 0 until collections.length()){val c=collections.getJSONObject(i);val cid=c.optString("id");chip(c.optString("name"),collectionFilter==cid){collectionFilter=cid;bookFilter=0;render()}}
                }
                if(currentArea==2)listOf("Alle","Fertig","In Arbeit","Entwürfe").forEachIndexed {index,name->filters.addView(button(name){hsFilter=index;render()}.apply {textSize=12f;minHeight=dp(44);setPadding(dp(12),dp(10),dp(12),dp(10));background=NativeDesign.touch(this@NativeActivity,if(hsFilter==index)NativeDesign.accentPanel else NativeDesign.panel,22);setTextColor(if(hsFilter==index)NativeDesign.ink else NativeDesign.muted);isSelected=hsFilter==index},LinearLayout.LayoutParams(-2,-2).apply {rightMargin=dp(6)})}
                val sortMode=prefs.getString("sort-books","recent") ?: "recent"
                val items=(0 until all.length()).map {all.getJSONObject(it)}.let {list->if(currentArea!=0)list else when(sortMode){
                    "title"->list.sortedBy {it.optString("title").lowercase()}
                    "added"->list.sortedByDescending {it.optString("created_at")}
                    else->list.sortedWith(compareByDescending<JSONObject> {it.optJSONObject("progress")?.optString("updatedAt") ?: ""}.thenByDescending {it.optString("created_at")})
                }}.filter {item->
                    val progress=item.optJSONObject("progress")
                    val hidden=item.optBoolean("is_hidden")
                    val inCollection=collectionFilter.isEmpty() || (item.optJSONArray("collectionIds") ?: JSONArray()).let {ids->(0 until ids.length()).any {ids.optString(it)==collectionFilter}}
                    val hsStatus=item.optString("status")
                    val matchesHs=currentArea!=2 || when(hsFilter){1->hsStatus=="completed";2->hsStatus=="running"||hsStatus=="queued";3->hsStatus!="completed"&&hsStatus!="running"&&hsStatus!="queued";else->true}
                    val matchesFilter=matchesHs && (currentArea!=0 || (inCollection && when {collectionFilter.isNotEmpty()->!hidden;bookFilter==3->hidden;hidden->false;bookFilter==0->true;bookFilter==2->progress?.optBoolean("completed")==true;else->progress!=null && !progress.optBoolean("completed") && (progress.optInt("chapterIndex")>0 || progress.optInt("segmentIndex")>0)}))
                    matchesFilter && listOf("title","author","name","topic").any {item.optString(it).contains(query.trim(),ignoreCase=true)}
                }
                if(currentArea==0 && query.isBlank() && bookFilter==0 && collectionFilter.isEmpty()){
                    val recent=items.filter {val p=it.optJSONObject("progress");p!=null && !p.optBoolean("completed") && !it.optBoolean("is_hidden") && it.optString("voice_id").isNotBlank() && it.optString("voice_id")!="null"}.maxByOrNull {it.optJSONObject("progress")!!.optString("updatedAt")}
                    if(recent!=null){
                        val p=recent.optJSONObject("progress")!!;val rid=recent.optString("id");val rtitle=recent.optString("title")
                        val card=LinearLayout(this).apply {gravity=Gravity.CENTER_VERTICAL;setPadding(dp(10),dp(10),dp(6),dp(10));background=NativeDesign.touch(this@NativeActivity,NativeDesign.panel,16);isClickable=true;isFocusable=true;contentDescription="Weiterhören $rtitle";setOnClickListener {pages.addLast {area=0;home()};detail(0,recent,rtitle)}}
                        val cover=NativeCover(this,rtitle,rid);card.addView(cover,LinearLayout.LayoutParams(dp(52),dp(72)));loadCover(cover,rid)
                        val words=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL;setPadding(dp(12),0,dp(8),0)}
                        words.addView(caption("WEITERHÖREN").apply {textSize=11f;setPadding(0,0,0,dp(2))})
                        words.addView(NativeDesign.text(this,rtitle,15,bold=true).apply {maxLines=1;ellipsize=TextUtils.TruncateAt.END})
                        words.addView(caption("Kapitel ${p.optInt("chapterIndex")+1} von ${recent.optInt("total_chapters",1)} · ${recent.optString("voice_id")}").apply {maxLines=1;ellipsize=TextUtils.TruncateAt.END;textSize=12f})
                        card.addView(words,LinearLayout.LayoutParams(0,-2,1f))
                        card.addView(iconButton("play","Weiterhören $rtitle") {listen(rid,rtitle,recent.optString("voice_id"),p.optInt("chapterIndex"),p.optInt("segmentIndex"))}.apply {background=NativeDesign.touch(this@NativeActivity,NativeDesign.accentPanel,24)})
                        results.addView(card,LinearLayout.LayoutParams(-1,-2).apply {bottomMargin=dp(14)})
                    }
                }
                results.addView(caption("${items.size} ${if(currentArea==0)"Bücher" else "Einträge"}"))
                if(items.isEmpty()){
                    results.addView(NativeIcon(this,areaIcons[currentArea],NativeDesign.border),LinearLayout.LayoutParams(dp(120),dp(120)).apply {gravity=Gravity.CENTER_HORIZONTAL;topMargin=dp(36)})
                    results.addView(label(if(query.isNotBlank())"Kein passender Titel" else "Hier ist noch Platz für Geschichten",22).apply {gravity=Gravity.CENTER})
                    results.addView(caption(if(query.isNotBlank())"Versuche einen anderen Suchbegriff." else when(currentArea){0->"Importiere ein Buch als PDF, EPUB, DOCX oder TXT.";1->"Lege eine Episode an und ordne Moderatoren zu.";2->"Hörspielprojekte werden im Web-Studio angelegt.";else->"Deine Stimmen erscheinen hier."}).apply {gravity=Gravity.CENTER})
                    if(query.isBlank() && currentArea==0)results.addView(button("Erstes Buch importieren") {pick("application/pdf","application/epub+zip","text/plain","application/vnd.openxmlformats-officedocument.wordprocessingml.document") {uri->importBook(uri)}}.apply {gravity=Gravity.CENTER;background=NativeDesign.touch(this@NativeActivity,NativeDesign.accentPanel)},LinearLayout.LayoutParams(-1,-2).apply {topMargin=dp(18)})
                    if(query.isBlank() && currentArea==1)results.addView(button("Erste Episode anlegen") {podcastForm(null){created->pages.addLast {area=1;home()};detail(1,created,created.optString("topic").ifBlank {"Neue Episode"})}}.apply {gravity=Gravity.CENTER;background=NativeDesign.touch(this@NativeActivity,NativeDesign.accentPanel)},LinearLayout.LayoutParams(-1,-2).apply {topMargin=dp(18)})
                    return
                }
                val gridWidth=(if(results.width>0)results.width else resources.displayMetrics.widthPixels-dp(if(navigationMode==2)208 else if(navigationMode==1)88 else 0)-dp(48))
                val columns=when {gridWidth>=dp(1050)->5;gridWidth>=dp(760)->4;gridWidth>=dp(510)->3;else->2}
                val cellWidth=gridWidth/columns
                val grid=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL}
                results.addView(grid,LinearLayout.LayoutParams(-1,-2))
                var rowView:LinearLayout?=null
                items.forEachIndexed {position,item->
                    val name=item.optString("title").ifBlank {item.optString("name").ifBlank {item.optString("topic","Ohne Titel")}}
                    val card=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL;setPadding(dp(5),dp(5),dp(5),dp(12));background=NativeDesign.touch(this@NativeActivity,Color.TRANSPARENT,14);isClickable=true;isFocusable=true;contentDescription=name;setOnClickListener {pages.addLast {area=currentArea;home()};detail(currentArea,item,name)};isLongClickable=true;setOnLongClickListener {cardMenu(currentArea,item,name);true}}
                    val cover=NativeCover(this,name,item.optString("id"));card.addView(cover,LinearLayout.LayoutParams(-1,((cellWidth-dp(10))*1.42).toInt()))
                    if(currentArea==0)loadCover(cover,item.optString("id"))
                    if(currentArea==2 && item.optJSONObject("binding")!=null)loadCover(cover,"hs:"+item.optString("id"),"/api/hoerspiele/projects/${android.net.Uri.encode(item.optString("id"))}/cover")
                    card.addView(NativeDesign.text(this,name,15,bold=true).apply {maxLines=2;ellipsize=TextUtils.TruncateAt.END;setPadding(0,dp(10),0,dp(3))})
                    val subtitle=item.optString("author").ifBlank {when(currentArea){1->podcastStatus(item.optString("status","draft"))+(item.optDouble("audio_duration",0.0).takeIf {it>0}?.let {" · ${(it/60).toInt()} Min"} ?: "");2->hoerspielStatus(item.optString("status"))+((item.optJSONArray("artifacts")?.takeIf {it.length()>0}?.getJSONObject(0)?.optJSONObject("audio")?.optLong("duration_ms") ?: 0L).takeIf {it>0}?.let {" · ${it/60000} Min"} ?: "");3->item.optString("language","Stimme");else->"Hörbuch"}}
                    card.addView(caption(subtitle).apply {maxLines=1;ellipsize=TextUtils.TruncateAt.END})
                    val progress=item.optJSONObject("progress")
                    if(currentArea==0 && progress!=null){val complete=progress.optBoolean("completed");val total=item.optInt("total_chapters",1).coerceAtLeast(1);val percent=if(complete)100 else progress.optInt("chapterIndex")*100/total
                        card.addView(ProgressBar(this,null,android.R.attr.progressBarStyleHorizontal).apply {max=100;this.progress=percent;progressTintList=android.content.res.ColorStateList.valueOf(NativeDesign.accent)},LinearLayout.LayoutParams(-1,dp(4)).apply {topMargin=dp(6)})
                        val offlineBook=(prefs.getStringSet("offline-chapters",emptySet()) ?: emptySet()).any {it.startsWith(item.optString("id")+":")}
                        card.addView(caption((if(complete)"Beendet" else "Kapitel ${progress.optInt("chapterIndex")+1} von $total")+(if(offlineBook)" · unterwegs" else "")).apply {textSize=12f})
                    }
                    if(position%columns==0){rowView=LinearLayout(this);grid.addView(rowView,LinearLayout.LayoutParams(-1,-2).apply {bottomMargin=dp(12)})}
                    rowView!!.addView(card,LinearLayout.LayoutParams(0,-2,1f))
                }
                val filled=items.size%columns
                if(filled!=0)for(i in filled until columns)rowView!!.addView(View(this),LinearLayout.LayoutParams(0,1,1f))
            }
            render();filtersReload={render()}
            search.addTextChangedListener(object:TextWatcher {override fun beforeTextChanged(s:CharSequence?,start:Int,count:Int,after:Int){};override fun onTextChanged(s:CharSequence?,start:Int,before:Int,count:Int){query=s.toString();render()};override fun afterTextChanged(s:Editable?){} })
        }
    }
    private fun detail(kind:Int,item:JSONObject,name:String) {
        destination={detail(kind,item,name)}
        reset(name,back=true)
        val id=item.optString("id")
        val revisionNow=generation
        val path=when(kind) {0->"/api/audiobooks/$id";1->"/api/podcasts/$id";2->"/api/hoerspiele/projects/$id";else->null}
        if(path==null) {content.addView(label(item.optString("description",name)));return}
        fetch(path,cache=true) {value ->
            val data=value as JSONObject
            val chapters=data.optJSONArray("chapters") ?: JSONArray()
            val hero=LinearLayout(this).apply {gravity=Gravity.CENTER_VERTICAL;setPadding(0,dp(12),0,dp(24))}
            val cover=NativeCover(this,data.optString("title",name),id)
            hero.addView(cover,LinearLayout.LayoutParams(dp(112),dp(164)).apply {rightMargin=dp(22)})
            if(kind==0)loadCover(cover,id)
            if(kind==2 && data.optJSONObject("binding")!=null)loadCover(cover,"hs:$id","/api/hoerspiele/projects/${android.net.Uri.encode(id)}/cover")
            val summary=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL}
            summary.addView(caption(when(kind){0->"HÖRBUCH";1->"PODCAST";else->"HÖRSPIEL"}))
            summary.addView(label(data.optString("title",name),25))
            val description=data.optString("author").ifBlank {data.optString("description")}
            if(description.isNotBlank())summary.addView(caption(description).apply {maxLines=4;ellipsize=TextUtils.TruncateAt.END})
            if(chapters.length()>0)summary.addView(caption("${chapters.length()} Kapitel"))
            hero.addView(summary,LinearLayout.LayoutParams(0,-2,1f));content.addView(hero)
            if(kind==0) {
                val progress=data.optJSONObject("progress")
                val chapterIndex=(progress?.optInt("chapterIndex") ?: 0).coerceIn(0,(chapters.length()-1).coerceAtLeast(0))

                val bookVoice=data.optString("voice_id").takeUnless {it=="null"}.orEmpty()
                val resuming=progress!=null && !progress.optBoolean("completed")
                if(chapters.length()>0) {
                    val listenText=if(resuming)"Weiterhören · Kapitel ${chapterIndex+1}" else "Buch anhören"
                    val row=LinearLayout(this).apply {gravity=Gravity.CENTER_VERTICAL}
                    val primary=LinearLayout(this).apply {gravity=Gravity.CENTER;background=NativeDesign.touch(this@NativeActivity,NativeDesign.accentPanel);isClickable=true;isFocusable=true;contentDescription=listenText;setPadding(dp(12),dp(10),dp(18),dp(10));setOnClickListener {listen(id,data.optString("title",name),bookVoice,if(resuming)chapterIndex else 0,if(resuming)progress!!.optInt("segmentIndex") else 0)}}
                    primary.addView(NativeIcon(this,"play",NativeDesign.ink),LinearLayout.LayoutParams(dp(30),dp(30)))
                    primary.addView(NativeDesign.text(this,listenText,15,bold=true).apply {setPadding(dp(8),0,0,0);maxLines=1;ellipsize=TextUtils.TruncateAt.END})
                    row.addView(primary,LinearLayout.LayoutParams(0,dp(56),1f).apply {rightMargin=dp(8)})
                    row.addView(button("Lesen") {pages.addLast {detail(kind,item,name)};reader(id,chapterIndex,name,bookVoice,if(resuming)progress!!.optInt("segmentIndex") else -1)}.apply {gravity=Gravity.CENTER;minHeight=dp(56)},LinearLayout.LayoutParams(-2,dp(56)))
                    content.addView(row,LinearLayout.LayoutParams(-1,-2).apply {bottomMargin=dp(12)})
                }
                headerAction("search","Im Buch suchen") {bookSearch(id,data.optString("title",name),bookVoice)}
                headerAction("more","Buch verwalten") {bookMenu(id,item,name,data)}
                val exports=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL};content.addView(exports)
                for(fmt in listOf("m4b","mp3"))worker.execute {val status=runCatching {api.request("/api/audiobooks/$id/export/status?format=$fmt") as JSONObject}.getOrNull();runOnUiThread {if(isDestroyed || generation!=revisionNow)return@runOnUiThread;if(status?.optBoolean("ready")==true)exports.addView(button("Export herunterladen · ${fmt.uppercase()} · ${status.optLong("size")/1048576} MB") {saveDownload("/api/audiobooks/$id/export/download?format=$fmt","${name.replace(Regex("[^\\p{L}\\p{N} _-]"),"_").take(80)}.$fmt")},LinearLayout.LayoutParams(-1,-2).apply {bottomMargin=dp(8)})}}
                data.optJSONObject("generation")?.let {job->if(job.optString("status")=="running")content.addView(caption("Vertonung läuft: ${job.optInt("done")} von ${job.optInt("total")} Abschnitten (${job.optString("voice_id")})")) else if(job.optString("error").isNotBlank())content.addView(caption("Letzte Vertonung: ${job.optString("error")}").apply {setTextColor(Color.rgb(255,176,176))})}
                if(bookVoice.isNotBlank()){val done=(0 until chapters.length()).count {chapters.getJSONObject(it).optBoolean("complete")};content.addView(caption("Stimme $bookVoice · $done von ${chapters.length()} Kapiteln vertont"))}
                content.addView(label("Kapitel",22).apply {setPadding(0,dp(24),0,dp(12))})
                for(i in 0 until chapters.length()) {
                    val chapter=chapters.getJSONObject(i)
                    val row=LinearLayout(this).apply {gravity=Gravity.CENTER_VERTICAL;setPadding(dp(12),dp(12),dp(8),dp(12));background=NativeDesign.touch(this@NativeActivity,if(i==chapterIndex)NativeDesign.panel else Color.TRANSPARENT,12);isClickable=true;isFocusable=true;setOnClickListener{pages.addLast {detail(kind,item,name)};reader(id,chapter.optInt("index",i),name,data.optString("voice_id").takeUnless {it=="null"}.orEmpty())}}
                    row.addView(NativeDesign.text(this,(i+1).toString().padStart(2,'0'),13,NativeDesign.muted).apply {gravity=Gravity.CENTER},LinearLayout.LayoutParams(dp(36),dp(42)))
                    val words=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL;setPadding(dp(8),0,0,0)}
                    words.addView(NativeDesign.text(this,chapter.optString("title","Kapitel ${i+1}"),16,bold=i==chapterIndex))
                    val offline=(prefs.getStringSet("offline-chapters",emptySet()) ?: emptySet()).contains("$id:${chapter.optInt("index",i)}")
                    words.addView(caption((if(chapter.optBoolean("complete"))"Audio vollständig" else if(chapter.optInt("cachedSegments")>0)"${chapter.optInt("cachedSegments")} von ${chapter.optInt("totalSegments")} Abschnitten vertont" else "Text · wird beim Hören vertont")+(if(offline)" · unterwegs verfügbar" else "")).apply {setPadding(0,dp(4),0,0);textSize=12f;if(offline)setTextColor(NativeDesign.accent)})
                    row.addView(words,LinearLayout.LayoutParams(0,-2,1f));row.addView(NativeIcon(this,"next"),LinearLayout.LayoutParams(dp(36),dp(42)))
                    content.addView(row,LinearLayout.LayoutParams(-1,-2).apply {bottomMargin=dp(4)})
                }
                content.addView(label("Lesezeichen",22).apply {setPadding(0,dp(24),0,dp(8))})
                val marks=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL};content.addView(marks)
                bookmarks(id,name,bookVoice,marks)
            }
            if(kind==2)hoerspielDetail(id,data,item,name)
            if(kind==1)podcastDetail(id,data,item,name)
        }
    }
    private fun reader(id:String,chapter:Int,name:String,voice:String,jumpTo:Int=-1) {
        destination={reader(id,chapter,name,voice)}
        reset(name,back=true)
        val textSize=prefs.getInt("reader-size",19)
        headerAction("text","Schriftgröße") {val sizes=intArrayOf(17,19,22,26);AlertDialog.Builder(this).setTitle("Schriftgröße").setSingleChoiceItems(arrayOf("Klein","Normal","Groß","Sehr groß"),sizes.indexOf(textSize).coerceAtLeast(0)){dialog,i->prefs.edit().putInt("reader-size",sizes[i]).apply();dialog.dismiss();reader(id,chapter,name,voice)}.setNegativeButton("Abbrechen",null).show()}
        if(voice.isNotBlank())headerAction("download","Kapitel für unterwegs laden") {prefetchChapter(id,name,voice,chapter)}
        window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        val follow=prefs.getBoolean("reader-follow",false)
        headerAction("follow",if(follow)"Mitscrollen ausschalten" else "Beim Hören mitscrollen") {prefs.edit().putBoolean("reader-follow",!follow).apply();Toast.makeText(this,if(follow)"Mitscrollen aus" else "Der Leser folgt jetzt der Wiedergabe",Toast.LENGTH_SHORT).show();reader(id,chapter,name,voice)}
        fetch("/api/audiobooks/$id/content?chapter=$chapter",cache=true) {value ->
            val data=value as JSONObject
            val totalChapters=data.optInt("totalChapters",chapter+1)
            content.addView(label(data.optJSONObject("chapter")?.optString("title") ?: "Kapitel ${chapter+1}",24))
            content.addView(caption("Kapitel ${chapter+1} von $totalChapters"))
            val segments=data.optJSONArray("segments") ?: JSONArray()
            content.addView(caption("Absatz gedrückt halten: Anhören, Lesezeichen oder Kopieren."))
            if(voice.isNotBlank())content.addView(button("Kapitel anhören") {listen(id,name,voice,chapter,0)}.apply {background=NativeDesign.touch(this@NativeActivity,NativeDesign.accentPanel);gravity=Gravity.CENTER},LinearLayout.LayoutParams(-1,-2).apply {topMargin=dp(6);bottomMargin=dp(12)})
            val paragraphs=HashMap<Int,TextView>()
            for(i in 0 until segments.length()) {
                val segment=segments.getJSONObject(i)
                val index=segment.optInt("index",i)
                val paragraph=label(segment.optString("text"),textSize).apply {typeface=android.graphics.Typeface.create("serif",android.graphics.Typeface.NORMAL);setLineSpacing(dp(6).toFloat(),1.28f);setPadding(dp(10),dp(8),dp(10),dp(12));isLongClickable=true}
                paragraph.setOnLongClickListener {
                    val choices=if(voice.isNotBlank())arrayOf("Ab hier anhören","Lesezeichen setzen","Text kopieren") else arrayOf("Lesezeichen setzen","Text kopieren")
                    AlertDialog.Builder(this).setTitle("Absatz ${i+1}").setItems(choices){_,which->when(choices[which]){
                        "Ab hier anhören"->listen(id,name,voice,chapter,index)
                        "Lesezeichen setzen"->bookmarkDialog(id,chapter,index,segment.optString("text"))
                        else-> {getSystemService(android.content.ClipboardManager::class.java).setPrimaryClip(android.content.ClipData.newPlainText("Absatz",segment.optString("text")));Toast.makeText(this,"Text kopiert",Toast.LENGTH_SHORT).show()}
                    }}.show();true
                }
                paragraphs[index]=paragraph
                content.addView(paragraph)
            }
            val navigation=LinearLayout(this).apply {gravity=Gravity.CENTER_VERTICAL;setPadding(0,dp(24),0,dp(8))}
            if(chapter>0)navigation.addView(button("← Kapitel $chapter") {pages.addLast {reader(id,chapter,name,voice)};reader(id,chapter-1,name,voice)}.apply {gravity=Gravity.CENTER},LinearLayout.LayoutParams(0,-2,1f).apply {rightMargin=dp(8)})
            if(chapter<totalChapters-1)navigation.addView(button("Kapitel ${chapter+2} →") {pages.addLast {reader(id,chapter,name,voice)};reader(id,chapter+1,name,voice)}.apply {gravity=Gravity.CENTER;background=NativeDesign.touch(this@NativeActivity,NativeDesign.accentPanel)},LinearLayout.LayoutParams(0,-2,1f))
            content.addView(navigation)
            if(jumpTo>=0)content.post {paragraphs[jumpTo]?.let {target->(content.parent as? ScrollView)?.smoothScrollTo(0,(target.top-dp(72)).coerceAtLeast(0));target.background=NativeDesign.shape(this@NativeActivity,NativeDesign.panel,10);target.postDelayed(Runnable {if(target.background!=null && NativePlaybackService.current?.track?.let {it.bookId==id && it.chapter==chapter && it.segment==jumpTo}!=true)target.background=null},2500)}}
            val revision=generation;var active=-1
            val follow=object:Runnable {override fun run(){
                if(isDestroyed || revision!=generation)return
                val track=NativePlaybackService.current?.track
                val now=if(track!=null && track.bookId==id && track.chapter==chapter)track.segment else -1
                if(now!=active){paragraphs[active]?.background=null;paragraphs[now]?.background=NativeDesign.shape(this@NativeActivity,NativeDesign.panel,10);active=now;if(now>=0 && prefs.getBoolean("reader-follow",false))paragraphs[now]?.let {t->(content.parent as? ScrollView)?.smoothScrollTo(0,(t.top-dp(140)).coerceAtLeast(0))}}
                content.postDelayed(this,500)
            }};content.post(follow)
        }
    }
    private fun playerScreen() {
        if(playerVisible)return
        destination?.let {pages.addLast(it)};reset("Wiedergabe",back=true);playerVisible=true
        var shownTrack:Track?=null
        val cover=NativeCover(this,"Deine nächste Geschichte","player")
        val compact=((content.parent as? View)?.height ?: resources.displayMetrics.heightPixels)<dp(640)
        content.addView(cover,LinearLayout.LayoutParams(dp(if(compact)150 else 220),dp(if(compact)204 else 300)).apply {gravity=Gravity.CENTER_HORIZONTAL;topMargin=dp(if(compact)4 else 12);bottomMargin=dp(if(compact)12 else 22)})
        val status=label("",25).apply {gravity=Gravity.CENTER};content.addView(status)
        val sub=caption("").apply {gravity=Gravity.CENTER;setPadding(0,0,0,dp(8))};content.addView(sub)
        val retryButton=button("Erneut versuchen") {NativePlaybackService.current?.retry()}.apply {gravity=Gravity.CENTER;visibility=View.GONE;background=NativeDesign.touch(this@NativeActivity,NativeDesign.accentPanel)};content.addView(retryButton,LinearLayout.LayoutParams(-1,-2).apply {bottomMargin=dp(8)})
        val slider=SeekBar(this).apply {progressTintList=android.content.res.ColorStateList.valueOf(NativeDesign.accent);thumbTintList=android.content.res.ColorStateList.valueOf(NativeDesign.accent)}
        content.addView(slider,LinearLayout.LayoutParams(-1,dp(48)))
        val chapterBar=ProgressBar(this,null,android.R.attr.progressBarStyleHorizontal).apply {max=1000;progressTintList=android.content.res.ColorStateList.valueOf(NativeDesign.muted);progressBackgroundTintList=android.content.res.ColorStateList.valueOf(NativeDesign.border);visibility=View.GONE;contentDescription="Fortschritt im Kapitel"}
        content.addView(chapterBar,LinearLayout.LayoutParams(-1,dp(3)).apply {setMargins(dp(16),0,dp(16),dp(6))})
        val positionText=caption("").apply {gravity=Gravity.CENTER};content.addView(positionText)
        var dragging=false
        slider.setOnSeekBarChangeListener(object:SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(bar:SeekBar,value:Int,user:Boolean){}
            override fun onStartTrackingTouch(bar:SeekBar){dragging=true}
            override fun onStopTrackingTouch(bar:SeekBar){NativePlaybackService.current?.seek(bar.progress.toLong()*1000);dragging=false}
        })
        val controls=LinearLayout(this).apply {gravity=Gravity.CENTER;setPadding(0,dp(16),0,dp(24))}
        controls.addView(iconButton("back","Vorheriger Abschnitt") {NativePlaybackService.current?.previous()})
        controls.addView(button("−15 s") {NativePlaybackService.current?.let {it.seek(it.position-15000)}}.apply {gravity=Gravity.CENTER;setPadding(0,0,0,0);maxLines=1;background=NativeDesign.touch(this@NativeActivity,Color.TRANSPARENT)},LinearLayout.LayoutParams(dp(64),dp(64)))
        val toggle=iconButton("play","Wiedergabe fortsetzen") {root.performHapticFeedback(android.view.HapticFeedbackConstants.CONTEXT_CLICK);NativePlaybackService.current?.let {if(it.playing)it.pause() else it.resume()}}.apply {background=NativeDesign.touch(this@NativeActivity,NativeDesign.accentPanel,40)}
        controls.addView(toggle,LinearLayout.LayoutParams(dp(80),dp(80)).apply {setMargins(dp(18),0,dp(18),0)})
        controls.addView(button("+30 s") {NativePlaybackService.current?.let {it.seek(it.position+30000)}}.apply {gravity=Gravity.CENTER;setPadding(0,0,0,0);maxLines=1;background=NativeDesign.touch(this@NativeActivity,Color.TRANSPARENT)},LinearLayout.LayoutParams(dp(64),dp(64)))
        controls.addView(iconButton("next","Nächster Abschnitt") {NativePlaybackService.current?.next()})
        content.addView(controls)
        val extras=LinearLayout(this).apply {gravity=Gravity.CENTER}
        val speedButton=button("Tempo · 1×") {AlertDialog.Builder(this).setTitle("Wiedergabetempo").setItems(arrayOf("0,75×","1×","1,25×","1,5×","2×")){_,which->NativePlaybackService.current?.speed(floatArrayOf(.75f,1f,1.25f,1.5f,2f)[which])}.show()}.apply {gravity=Gravity.CENTER}
        val sleepButton=button("Schlummer") {AlertDialog.Builder(this).setTitle("Schlummer-Timer").setItems(arrayOf("Aus","Am Kapitelende","15 Minuten","30 Minuten","45 Minuten","60 Minuten")){_,which->NativePlaybackService.current?.let {if(which==1)it.sleepUntilChapterEnd() else it.sleep(intArrayOf(0,0,15,30,45,60)[which])}}.show()}.apply {gravity=Gravity.CENTER}
        extras.addView(speedButton,LinearLayout.LayoutParams(0,-2,1f).apply {rightMargin=dp(8)});extras.addView(sleepButton,LinearLayout.LayoutParams(0,-2,1f))
        content.addView(extras,LinearLayout.LayoutParams(-1,-2))
        val revision=generation
        var lastPlaying:Boolean?=null
        val update=object:Runnable {override fun run(){
            if(isDestroyed || revision!=generation)return
            val service=NativePlaybackService.current
            status.text=service?.error ?: service?.title ?: "Wähle eine Geschichte aus deiner Bibliothek."
            val nowTrack=service?.track
            if(nowTrack!==shownTrack){shownTrack=nowTrack;cover.bookTitle=nowTrack?.title ?: "Deine nächste Geschichte";cover.bitmap=null;if(nowTrack!=null && nowTrack.bookId.isNotBlank()){loadCover(cover,nowTrack.bookId);if(headerActions.childCount==0){headerAction("bookmark","Lesezeichen setzen") {NativePlaybackService.current?.track?.let {t->if(t.bookId.isNotBlank())bookmarkDialog(t.bookId,t.chapter,t.segment,t.subtitle)}};headerAction("list","Kapitel") {playerChapters(nowTrack)}}}}
            val (partIndex,partCount)=service?.chapterPosition ?: (0 to 0)
            sub.text=service?.subtitle.orEmpty()+(if(partCount>0)" · Abschnitt $partIndex von $partCount" else "")
            chapterBar.visibility=if(partCount>1)View.VISIBLE else View.GONE;if(partCount>1)chapterBar.progress=((partIndex-1)*1000/partCount)+(if(slider.max>0)(slider.progress*1000/slider.max)/partCount else 0)
            val rate=service?.rate ?: 1f;speedButton.text="Tempo · ${if(rate==rate.toInt().toFloat())"${rate.toInt()}×" else "${rate.toString().replace('.',',')}×"}"
            val remaining=service?.sleepRemainingMs ?: 0L
            sleepButton.text=when {service?.sleepsAtChapterEnd==true->"Schlummer · Kapitelende";remaining>0->"Schlummer · ${(remaining/60000)+1} Min";else->"Schlummer"}
            if(!dragging){slider.max=((service?.duration ?: 0)/1000).toInt();slider.progress=((service?.position ?: 0)/1000).toInt()}
            val left=(slider.max-slider.progress).coerceAtLeast(0)
            positionText.text="${slider.progress/60}:${(slider.progress%60).toString().padStart(2,'0')}   /   ${slider.max/60}:${(slider.max%60).toString().padStart(2,'0')}"+(if(slider.max>0)"   ·   noch ${left/60}:${(left%60).toString().padStart(2,'0')}" else "")
            retryButton.visibility=if(service?.error!=null)View.VISIBLE else View.GONE
            val playing=service?.playing==true
            if(lastPlaying!=playing){lastPlaying=playing;toggle.removeAllViews();toggle.addView(NativeIcon(this@NativeActivity,if(playing)"pause" else "play",NativeDesign.ink),FrameLayout.LayoutParams(-1,-1));toggle.contentDescription=if(playing)"Wiedergabe pausieren" else "Wiedergabe fortsetzen"}
            status.postDelayed(this,500)
        }};status.post(update)
    }
    private fun pick(vararg mimes:String,done:(android.net.Uri)->Unit) {pendingPick=done;runCatching {picker.launch(arrayOf(*mimes))}.onFailure {pendingPick=null;Toast.makeText(this,"Keine Dateiauswahl verfügbar.",Toast.LENGTH_LONG).show()}}
    private fun busy(message:String):AlertDialog = AlertDialog.Builder(this).setMessage(message).setCancelable(false).create().also {it.show()}
    private fun fail(error:Throwable) {
        if(error is LoginRequired)AlertDialog.Builder(this).setTitle("Anmeldung nötig").setMessage(error.message).setPositiveButton("Anmelden"){_,_->login()}.setNegativeButton("Schließen",null).show()
        else AlertDialog.Builder(this).setTitle("Aktion fehlgeschlagen").setMessage(error.message ?: "Unbekannter Fehler").setPositiveButton("OK",null).show()
    }
    /** Segment tracks of one chapter plus the chapter count, so the queue can continue into the next chapter. */
    private fun chapterTracks(id:String,name:String,voice:String,chapter:Int):Pair<List<Track>,Int> {
        val data=requestCached("/api/audiobooks/$id/content?chapter=$chapter") as JSONObject
        val total=data.optInt("totalChapters",chapter+1)
        val chapterTitle=data.optJSONObject("chapter")?.optString("title")?.takeIf {it.isNotBlank()} ?: "Kapitel ${chapter+1}"
        val segments=data.optJSONArray("segments") ?: JSONArray()
        val tracks=(0 until segments.length()).map {i->val s=segments.getJSONObject(i);val index=s.optInt("index",i)
            Track(NavigationPolicy.ORIGIN+"/api/audiobooks/$id/audio-live/${android.net.Uri.encode(voice)}/$chapter/$index",name,chapterTitle,id,chapter,index,chapter>=total-1 && i==segments.length()-1,voice)}
        return tracks to total
    }
    /** Continuous listening from a position: the queue follows into later chapters and reports progress to the server. */
    private fun listen(id:String,name:String,voice:String,chapter:Int,segment:Int) {
        if(voice.isBlank()){Toast.makeText(this,"Bitte zuerst unter „Buch verwalten“ eine Erzählstimme auswählen.",Toast.LENGTH_LONG).show();return}
        val dialog=busy("Kapitel wird vorbereitet …")
        worker.execute {
            val result=runCatching {chapterTracks(id,name,voice,chapter)}
            runOnUiThread {
                if(isDestroyed)return@runOnUiThread
                dialog.dismiss()
                result.onSuccess {(tracks,total)->
                    if(tracks.isEmpty()){Toast.makeText(this,"Dieses Kapitel hat keinen Text.",Toast.LENGTH_LONG).show();return@onSuccess}
                    val start=tracks.indexOfFirst {it.segment>=segment}.coerceAtLeast(0)
                    runCatching {
                        val open=android.content.Intent(android.content.Intent.ACTION_VIEW,android.net.Uri.parse("${NavigationPolicy.ORIGIN}/audiobooks/${android.net.Uri.encode(id)}")).setClass(this,NativeActivity::class.java)
                        getSystemService(android.content.pm.ShortcutManager::class.java).dynamicShortcuts=listOf(android.content.pm.ShortcutInfo.Builder(this,"resume").setShortLabel("Weiterhören").setLongLabel("Weiterhören: ${name.take(40)}").setIcon(android.graphics.drawable.Icon.createWithResource(this,R.drawable.ic_play)).setIntent(open).build())
                    }
                    NativePlaybackService.playQueue(this,tracks,start) {last->
                        var next=last.chapter+1
                        while(next<total){val (more,_)=chapterTracks(id,name,voice,next);if(more.isNotEmpty())return@playQueue more;next++}
                        emptyList()
                    }
                    playerScreen()
                }.onFailure {fail(it)}
            }
        }
    }
    private fun studio() {
        destination={studio()}
        reset("Sprachstudio")
        val text=EditText(this).apply {hint="Was soll gesprochen werden?";textSize=17f;setTextColor(textColor);setHintTextColor(NativeDesign.muted);gravity=Gravity.TOP;minLines=4;inputType=android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE or android.text.InputType.TYPE_TEXT_FLAG_CAP_SENTENCES;setPadding(dp(16),dp(14),dp(16),dp(14));background=NativeDesign.shape(this@NativeActivity,NativeDesign.panel,14);setText(prefs.getString("studio-text",""));contentDescription="Text zum Sprechen";imeOptions=imeOptions or android.view.inputmethod.EditorInfo.IME_FLAG_NO_EXTRACT_UI or android.view.inputmethod.EditorInfo.IME_FLAG_NO_FULLSCREEN;}
        text.addTextChangedListener(object:TextWatcher {override fun beforeTextChanged(s:CharSequence?,start:Int,count:Int,after:Int){};override fun onTextChanged(s:CharSequence?,start:Int,before:Int,count:Int){prefs.edit().putString("studio-text",s.toString()).apply()};override fun afterTextChanged(s:Editable?){} })
        content.addView(text,LinearLayout.LayoutParams(-1,-2).apply {topMargin=dp(14);bottomMargin=dp(10)})
        val voiceId=prefs.getString("studio-voice","default") ?: "default";val voiceName=prefs.getString("studio-voice-name","Standardstimme") ?: "Standardstimme"
        val row=LinearLayout(this).apply {gravity=Gravity.CENTER_VERTICAL}
        row.addView(button("Stimme · $voiceName") {chooseVoice(voiceId) {id,n->prefs.edit().putString("studio-voice",id).putString("studio-voice-name",n).apply();studio()}}.apply {maxLines=1;ellipsize=TextUtils.TruncateAt.END},LinearLayout.LayoutParams(0,-2,1f).apply {rightMargin=dp(8)})
        row.addView(button("Sprechen") {speak(text.text.toString(),voiceId,voiceName)}.apply {background=NativeDesign.touch(this@NativeActivity,NativeDesign.accentPanel)},LinearLayout.LayoutParams(-2,-2))
        content.addView(row,LinearLayout.LayoutParams(-1,-2).apply {bottomMargin=dp(8)})
        content.addView(label("Clips",22).apply {setPadding(0,dp(20),0,dp(6))})
        val clips=File(filesDir,"clips").listFiles()?.filter {it.isFile && it.extension in setOf("wav","mp3")}?.sortedByDescending {it.name} ?: emptyList()
        if(clips.isEmpty())content.addView(caption("Gesprochene Clips erscheinen hier und bleiben auf dem Gerät."))
        val stamp=java.text.SimpleDateFormat("d. MMM yyyy, HH:mm",java.util.Locale.GERMAN)
        for(clip in clips.take(30)) {
            val parts=clip.nameWithoutExtension.split("__",limit=2);val at=parts[0].toLongOrNull() ?: clip.lastModified();val clipVoice=parts.getOrNull(1)?.replace('_',' ') ?: "Clip"
            val when_=stamp.format(java.util.Date(at))
            val spoken=runCatching {File(clip.parentFile,clip.nameWithoutExtension+".txt").readText()}.getOrDefault("")
            val line=LinearLayout(this).apply {gravity=Gravity.CENTER_VERTICAL;setPadding(dp(12),dp(10),dp(8),dp(10));background=NativeDesign.touch(this@NativeActivity,Color.TRANSPARENT,12);isClickable=true;isFocusable=true;isLongClickable=true;contentDescription="Clip $clipVoice, $when_"
                setOnClickListener {NativePlaybackService.play(this@NativeActivity,android.net.Uri.fromFile(clip).toString(),clipVoice,when_);playerScreen()}
                setOnLongClickListener {AlertDialog.Builder(this@NativeActivity).setTitle(clipVoice).setItems(arrayOf("Abspielen","Teilen","Text erneut verwenden","Löschen")){_,which->when(which){0->{NativePlaybackService.play(this@NativeActivity,android.net.Uri.fromFile(clip).toString(),clipVoice,when_);playerScreen()};1->shareClip(clip,"$clipVoice · $when_");2->{prefs.edit().putString("studio-text",spoken).apply();studio()};else->{clip.delete();File(clip.parentFile,clip.nameWithoutExtension+".txt").delete();studio()}}}.show();true}}
            line.addView(NativeIcon(this,"play",NativeDesign.accent),LinearLayout.LayoutParams(dp(36),dp(42)))
            val words=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL;setPadding(dp(8),0,0,0)}
            words.addView(NativeDesign.text(this,spoken.ifBlank {clipVoice},16).apply {maxLines=2;ellipsize=TextUtils.TruncateAt.END});words.addView(caption("$clipVoice · $when_ · ${clip.length()/1024} kB").apply {setPadding(0,dp(3),0,0);textSize=12f})
            line.addView(words,LinearLayout.LayoutParams(0,-2,1f));content.addView(line)
        }
        content.addView(label("Werkzeuge",22).apply {setPadding(0,dp(20),0,dp(6))})
        action("Aufnahme transkribieren") {pick("audio/*","video/*") {uri->transcribeDocument(uri)}}
        action("Stimme aus Aufnahme klonen") {pick("audio/*") {uri->cloneDocument(uri)}}
        content.addView(label("Stimmen",22).apply {setPadding(0,dp(20),0,dp(6))})
        content.addView(caption("Antippen für eine Hörprobe, gedrückt halten für weitere Aktionen."))
        fetch("/api/voices") {value->
            val voices=array(value,"voices")
            for(i in 0 until voices.length()) {
                val voice=voices.getJSONObject(i);val id=voice.optString("id").ifBlank {voice.optString("voice_id")};val n=voice.optString("name",id)
                val engine=voice.optString("engine").ifBlank {voice.optString("source")}
                val line=LinearLayout(this).apply {gravity=Gravity.CENTER_VERTICAL;setPadding(dp(12),dp(10),dp(8),dp(10));background=NativeDesign.touch(this@NativeActivity,Color.TRANSPARENT,12);isClickable=true;isFocusable=true;isLongClickable=true;contentDescription="Stimme $n"
                    setOnClickListener {previewVoice(id,n)}
                    setOnLongClickListener {AlertDialog.Builder(this@NativeActivity).setTitle(n).setItems(arrayOf("Hörprobe","Für das Studio verwenden","Stimme löschen")){_,which->when(which){
                        0->previewVoice(id,n)
                        1->{prefs.edit().putString("studio-voice",id).putString("studio-voice-name",n).apply();studio()}
                        else->AlertDialog.Builder(this@NativeActivity).setTitle("Stimme löschen").setMessage("$n dauerhaft löschen?").setNegativeButton("Abbrechen",null).setPositiveButton("Löschen"){_,_->mutate("/api/voices/${android.net.Uri.encode(id)}","DELETE",null){studio()}}.show()
                    }}.show();true}}
                line.addView(NativeIcon(this,"studio",if(id==voiceId)NativeDesign.accent else NativeDesign.muted),LinearLayout.LayoutParams(dp(36),dp(42)))
                val words=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL;setPadding(dp(8),0,0,0)}
                words.addView(NativeDesign.text(this,n,16,bold=id==voiceId));words.addView(caption("$engine · ${voice.optString("language","Auto")}").apply {setPadding(0,dp(3),0,0);textSize=12f})
                line.addView(words,LinearLayout.LayoutParams(0,-2,1f));line.addView(NativeIcon(this,"more"),LinearLayout.LayoutParams(dp(36),dp(42)))
                content.addView(line)
            }
        }
    }
    private fun speak(text:String,voice:String,voiceName:String) {
        if(text.isBlank()){Toast.makeText(this,"Bitte zuerst einen Text eingeben.",Toast.LENGTH_SHORT).show();return}
        val dialog=busy("Wird gesprochen …")
        worker.execute {
            val result=runCatching {
                val audio=api.bytes("/api/generate","POST",JSONObject().put("text",text).put("voice_id",voice).put("response_format","wav"))
                if(!audio.contentType.startsWith("audio/"))throw Exception("Der Server hat kein Audio geliefert.")
                val dir=File(filesDir,"clips").apply {mkdirs()}
                File(dir,"${System.currentTimeMillis()}__${voiceName.replace(Regex("[^\\p{L}\\p{N}-]"),"_")}.${if(audio.contentType.contains("mpeg"))"mp3" else "wav"}").also {it.writeBytes(audio.bytes);runCatching {File(dir,it.nameWithoutExtension+".txt").writeText(text)}}
            }
            runOnUiThread {if(isDestroyed)return@runOnUiThread;dialog.dismiss();result.onSuccess {file->NativePlaybackService.play(this,android.net.Uri.fromFile(file).toString(),voiceName,text.take(80));studio()}.onFailure {fail(it)}}
        }
    }
    private fun readDocument(uri:android.net.Uri,limit:Int):Triple<String,String,ByteArray> {
        var name="aufnahme"
        contentResolver.query(uri,arrayOf(android.provider.OpenableColumns.DISPLAY_NAME),null,null,null)?.use {if(it.moveToFirst())name=it.getString(0) ?: name}
        val mime=contentResolver.getType(uri) ?: "application/octet-stream"
        val bytes=contentResolver.openInputStream(uri)?.use {it.readBytesWithLimit(limit)} ?: throw Exception("Die Datei konnte nicht gelesen werden.")
        return Triple(name,mime,bytes)
    }
    private fun transcribeDocument(uri:android.net.Uri) {
        val dialog=busy("Transkription läuft … Das kann einige Minuten dauern.")
        worker.execute {
            val result=runCatching {val (n,mime,bytes)=readDocument(uri,200*1024*1024);api.upload("/api/transcribe",emptyMap(),"file",n,mime,bytes) as JSONObject}
            runOnUiThread {
                if(isDestroyed)return@runOnUiThread
                dialog.dismiss()
                result.onSuccess {data->
                    val transcript=data.optString("text").trim()
                    val view=TextView(this).apply {text=transcript.ifBlank {"(Keine Sprache erkannt)"};setTextIsSelectable(true);textSize=16f;setPadding(dp(24),dp(12),dp(24),dp(12))}
                    AlertDialog.Builder(this).setTitle("Transkript · ${data.optString("language","de")}").setView(ScrollView(this).apply {addView(view)}).setNegativeButton("Schließen",null)
                        .setPositiveButton("Kopieren"){_,_->getSystemService(android.content.ClipboardManager::class.java).setPrimaryClip(android.content.ClipData.newPlainText("Transkript",transcript));Toast.makeText(this,"Transkript kopiert",Toast.LENGTH_SHORT).show()}
                        .setNeutralButton("Ins Studio"){_,_->prefs.edit().putString("studio-text",transcript).apply();studio()}.show()
                }.onFailure {fail(it)}
            }
        }
    }
    private fun cloneDocument(uri:android.net.Uri) {
        val input=EditText(this).apply {hint="Name der Stimme";imeOptions=imeOptions or android.view.inputmethod.EditorInfo.IME_FLAG_NO_EXTRACT_UI or android.view.inputmethod.EditorInfo.IME_FLAG_NO_FULLSCREEN;inputType=android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_FLAG_CAP_WORDS}
        val box=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL;setPadding(dp(20),dp(8),dp(20),0);addView(input)}
        AlertDialog.Builder(this).setTitle("Stimme klonen").setMessage("3–10 Sekunden saubere Sprache ergeben die beste Stimme. Das Transkript wird automatisch erstellt.").setView(box).setNegativeButton("Abbrechen",null).setPositiveButton("Klonen"){_,_->
            val n=input.text.toString().trim()
            if(n.isEmpty()){Toast.makeText(this,"Bitte einen Namen eingeben.",Toast.LENGTH_SHORT).show();return@setPositiveButton}
            val dialog=busy("Stimme wird geklont …")
            worker.execute {
                val result=runCatching {val (file,mime,bytes)=readDocument(uri,50*1024*1024);api.upload("/api/voices/clone",mapOf("name" to n,"language" to "German","auto_transcribe" to "true"),"ref_audio",file,mime,bytes)}
                runOnUiThread {if(isDestroyed)return@runOnUiThread;dialog.dismiss();result.onSuccess {Toast.makeText(this,"Stimme „$n“ angelegt",Toast.LENGTH_SHORT).show();studio()}.onFailure {fail(it)}}
            }
        }.show()
    }
    private fun previewVoice(id:String,name:String) {
        val dialog=busy("Hörprobe wird geladen …")
        worker.execute {
            val result=runCatching {
                val audio=api.bytes("/api/voices/${android.net.Uri.encode(id)}/audio")
                if(!audio.contentType.startsWith("audio/"))throw Exception("Keine Hörprobe verfügbar.")
                File(File(cacheDir,"previews").apply {mkdirs()},"${id.replace(Regex("[^\\p{L}\\p{N}-]"),"_")}.wav").also {it.writeBytes(audio.bytes)}
            }
            runOnUiThread {if(isDestroyed)return@runOnUiThread;dialog.dismiss();result.onSuccess {file->NativePlaybackService.play(this,android.net.Uri.fromFile(file).toString(),name,"Hörprobe")}.onFailure {fail(it)}}
        }
    }
    private fun hoerspielStatus(status:String)=when(status){"completed"->"Fertiggestellt";"draft"->"Entwurf";"running"->"In Produktion";"queued"->"Wartet";"failed"->"Fehlgeschlagen";else->status.ifBlank {"Hörspiel"}}
    /** Quick actions from the shelf without opening the detail screen. */
    private fun cardMenu(kind:Int,item:JSONObject,name:String) {
        val id=item.optString("id")
        when(kind) {
            0->{val voice=item.optString("voice_id").takeUnless {it=="null"}.orEmpty();val p=item.optJSONObject("progress");val resuming=p!=null && !p.optBoolean("completed")
                AlertDialog.Builder(this).setTitle(name).setItems(arrayOf(if(resuming)"Weiterhören · Kapitel ${p!!.optInt("chapterIndex")+1}" else "Buch anhören","Lesen","Details")){_,which->when(which){
                    0->listen(id,name,voice,if(resuming)p!!.optInt("chapterIndex") else 0,if(resuming)p!!.optInt("segmentIndex") else 0)
                    1->{pages.addLast {area=0;home()};reader(id,if(resuming)p!!.optInt("chapterIndex") else 0,name,voice,if(resuming)p!!.optInt("segmentIndex") else -1)}
                    else->{pages.addLast {area=0;home()};detail(0,item,name)}}}.show()}
            1->{val hasAudio=!item.isNull("audio_path") && item.optString("audio_path").isNotBlank()
                AlertDialog.Builder(this).setTitle(name).setItems(if(hasAudio)arrayOf("Episode anhören","Details","Löschen") else arrayOf("Details","Löschen")){_,which->val choice=if(hasAudio)which else which+1;when(choice){
                    0->{NativePlaybackService.play(this,NavigationPolicy.ORIGIN+"/api/podcasts/$id/audio/stream",name,"Podcast");playerScreen()}
                    1->{pages.addLast {area=1;home()};detail(1,item,name)}
                    else->AlertDialog.Builder(this).setTitle("Episode löschen").setMessage("„$name“ löschen?").setNegativeButton("Abbrechen",null).setPositiveButton("Löschen"){_,_->mutate("/api/podcasts/$id","DELETE",null){home()}}.show()}}.show()}
            2->{val artifact=item.optJSONArray("artifacts")?.takeIf {it.length()>0}?.getJSONObject(0)
                AlertDialog.Builder(this).setTitle(name).setItems(if(artifact!=null)arrayOf("Anhören","Herunterladen","Details") else arrayOf("Details")){_,which->val choice=if(artifact!=null)which else 2;when(choice){
                    0->{NativePlaybackService.play(this,NavigationPolicy.ORIGIN+"/api/hoerspiele/artifacts/${artifact!!.getString("id")}/content",name,"Hörspiel");playerScreen()}
                    1->saveDownload("/api/hoerspiele/artifacts/${artifact!!.getString("id")}/content",artifact.optString("filename"))
                    else->{pages.addLast {area=2;home()};detail(2,item,name)}}}.show()}
            else->{val n=item.optString("name",id);AlertDialog.Builder(this).setTitle(n).setItems(arrayOf("Hörprobe","Für das Studio verwenden")){_,which->if(which==0)previewVoice(id,n) else {prefs.edit().putString("studio-voice",id).putString("studio-voice-name",n).apply();Toast.makeText(this,"Studio-Stimme: $n",Toast.LENGTH_SHORT).show()}}.show()}
        }
    }
    private fun podcastStatus(status:String)=when(status){"draft"->"Entwurf";"generating_script"->"Skript wird erzeugt …";"script_ready"->"Skript bereit";"generating_audio"->"Audio wird erzeugt …";"ready"->"Fertig";"cancelled"->"Abgebrochen";"error"->"Fehler";else->status}
    private fun podcastDetail(id:String,data:JSONObject,item:JSONObject,name:String) {
        val refresh={detail(1,item,name)}
        val status=data.optString("status","draft")
        content.addView(caption("${podcastStatus(status)} · ${when(data.optString("format")){"monolog"->"Monolog";"custom"->"Frei";else->"Dialog"}} · ${when(data.optString("duration")){"short"->"kurz";"long"->"lang";else->"mittel"}} · ${data.optString("language","de")}"))
        data.optString("error_message").takeIf {it.isNotBlank() && it!="null"}?.let {content.addView(caption(it).apply {setTextColor(Color.rgb(255,176,176))})}
        val busyNow=status=="generating_script"||status=="generating_audio"
        val hasAudio=!data.isNull("audio_path") && data.optString("audio_path").isNotBlank()
        if(hasAudio) {
            content.addView(button("Episode anhören") {NativePlaybackService.play(this,NavigationPolicy.ORIGIN+"/api/podcasts/$id/audio/stream",name,"Podcast");playerScreen()}.apply {background=NativeDesign.touch(this@NativeActivity,NativeDesign.accentPanel);gravity=Gravity.CENTER},LinearLayout.LayoutParams(-1,-2).apply {bottomMargin=dp(8)})
            if(data.optBoolean("audio_stale"))content.addView(caption("Das Skript wurde seit der letzten Audioerzeugung geändert."))
        }
        val hosts=data.optJSONArray("hosts") ?: JSONArray()
        var sourceCount=-1
        headerAction("more","Episode verwalten") {
            AlertDialog.Builder(this).setTitle(name).setItems(arrayOf("Thema und Format bearbeiten","Moderatoren zuordnen","Audio herunterladen","Episode löschen")){_,which->when(which){
                0->podcastForm(data){refresh()}
                1->assignHosts(id,hosts){refresh()}
                2->if(hasAudio)saveDownload("/api/podcasts/$id/audio/download","${name.replace(Regex("[^\\p{L}\\p{N} _-]"),"_").take(60)}.${data.optString("audio_format","mp3")}") else Toast.makeText(this,"Noch kein Audio vorhanden.",Toast.LENGTH_SHORT).show()
                else->AlertDialog.Builder(this).setTitle("Episode löschen").setMessage("„$name“ mit Quellen, Skript und Audio löschen?").setNegativeButton("Abbrechen",null).setPositiveButton("Löschen"){_,_->mutate("/api/podcasts/$id","DELETE",null){pages.clear();area=1;home()}}.show()
            }}.show()
        }
        content.addView(label("Besetzung",22).apply {setPadding(0,dp(20),0,dp(6))})
        if(hosts.length()==0)content.addView(caption("Noch keine Moderatoren. Ordne unter „Episode verwalten“ Stimmen zu."))
        for(i in 0 until hosts.length()) {val h=hosts.getJSONObject(i);val hv=h.optString("voice_id").takeIf {it.isNotBlank() && it!="null"}
            content.addView(caption("${h.optString("name")} · ${if(h.optString("role")=="expert")"Experte" else "Moderation"} · ${hv ?: "ohne Stimme"}${if(hv!=null)" · Hörprobe" else ""}").apply {if(hv!=null){isClickable=true;isFocusable=true;background=NativeDesign.touch(this@NativeActivity,Color.TRANSPARENT,10);setPadding(dp(8),dp(8),dp(8),dp(8));setOnClickListener {previewVoice(hv,h.optString("name"))}}})}
        content.addView(label("Quellen",22).apply {setPadding(0,dp(20),0,dp(6))})
        val sourceBox=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL};content.addView(sourceBox)
        fetch("/api/podcasts/$id/sources",sourceBox) {value->
            val sources=array(value,"sources");sourceCount=sources.length()
            if(sources.length()==0)sourceBox.addView(caption("Füge Text, einen Link oder eine Datei hinzu, bevor das Skript entsteht."))
            for(i in 0 until sources.length()) {
                val s=sources.getJSONObject(i);val sid=s.optString("id")
                val row=LinearLayout(this).apply {gravity=Gravity.CENTER_VERTICAL;setPadding(dp(12),dp(10),dp(8),dp(10));background=NativeDesign.touch(this@NativeActivity,Color.TRANSPARENT,12);isClickable=true;isFocusable=true;contentDescription="Quelle ${s.optString("title")}"
                    setOnClickListener {AlertDialog.Builder(this@NativeActivity).setTitle(s.optString("title")).setItems(arrayOf("Neu verarbeiten","Quelle entfernen")){_,which->if(which==0)mutate("/api/podcasts/$id/sources/$sid/reprocess","POST",null){refresh()} else mutate("/api/podcasts/$id/sources/$sid","DELETE",null){refresh()}}.show()}}
                val words=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL}
                words.addView(NativeDesign.text(this,s.optString("title").ifBlank {"Quelle"},16).apply {maxLines=2;ellipsize=TextUtils.TruncateAt.END})
                words.addView(caption("${when(s.optString("type")){"url"->"Link";"file"->"Datei";else->"Text"}} · ${when(s.optString("status")){"processed"->"${s.optInt("chunk_count")} Abschnitte";"failed"->"Fehler: ${s.optString("error_message")}";else->"wird verarbeitet …"}}").apply {setPadding(0,dp(3),0,0);textSize=12f})
                row.addView(words,LinearLayout.LayoutParams(0,-2,1f));row.addView(NativeIcon(this,"more"),LinearLayout.LayoutParams(dp(36),dp(42)));sourceBox.addView(row)
            }
        }
        if(!busyNow) {
            val adders=LinearLayout(this)
            adders.addView(button("Text") {textSourceDialog(id){refresh()}}.apply {gravity=Gravity.CENTER},LinearLayout.LayoutParams(0,-2,1f).apply {rightMargin=dp(6)})
            adders.addView(button("Link") {urlSourceDialog(id){refresh()}}.apply {gravity=Gravity.CENTER},LinearLayout.LayoutParams(0,-2,1f).apply {rightMargin=dp(6)})
            adders.addView(button("Datei") {pick("application/pdf","text/plain","application/epub+zip","application/vnd.openxmlformats-officedocument.wordprocessingml.document") {uri->uploadSource(id,uri){refresh()}}}.apply {gravity=Gravity.CENTER},LinearLayout.LayoutParams(0,-2,1f))
            content.addView(adders,LinearLayout.LayoutParams(-1,-2).apply {topMargin=dp(8)})
        }
        content.addView(label("Skript",22).apply {setPadding(0,dp(20),0,dp(6))})
        val script=data.optJSONObject("script")
        val segments=script?.optJSONArray("segments") ?: JSONArray()
        if(busyNow) {content.addView(caption("Die Produktion läuft auf dem Server."));action("Stand aktualisieren") {refresh()}}
        else {
            action(if(segments.length()==0)"Skript erzeugen" else "Skript neu erzeugen") {
                if(hosts.length()==0){Toast.makeText(this,"Bitte zuerst Moderatoren zuordnen.",Toast.LENGTH_LONG).show();return@action}
                if(sourceCount==0){Toast.makeText(this,"Bitte zuerst eine Quelle hinzufügen.",Toast.LENGTH_LONG).show();return@action}
                produce("/api/podcasts/$id/script/generate","Skript wird erzeugt …"){refresh()}
            }
            if(segments.length()>0)action(if(hasAudio)"Audio neu erzeugen" else "Audio erzeugen") {produce("/api/podcasts/$id/audio/generate"+(if(hasAudio)"?force=true" else ""),"Audio wird erzeugt …"){refresh()}}
        }
        if(segments.length()>0)content.addView(caption("${script!!.optInt("total_words")} Wörter · ca. ${(script.optDouble("estimated_duration",0.0)/60).toInt()} Min · Segment antippen zum Bearbeiten"))
        for(i in 0 until segments.length()) {
            val seg=segments.getJSONObject(i);val type=seg.optString("type","speech")
            val block=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL;setPadding(dp(12),dp(10),dp(12),dp(10));background=NativeDesign.touch(this@NativeActivity,Color.TRANSPARENT,12);isClickable=!busyNow;isFocusable=true;setOnClickListener {editSegment(id,seg){refresh()}}}
            block.addView(caption(when(type){"speech"->seg.optString("speaker","?");"reaction"->"${seg.optString("speaker","?")} · Reaktion";"pause"->"Pause";else->type}).apply {textSize=12f;setPadding(0,0,0,dp(2))})
            block.addView(NativeDesign.text(this,if(type=="pause")"${seg.optInt("duration_ms",600)} ms" else seg.optString("text"),16).apply {setLineSpacing(dp(3).toFloat(),1f)})
            content.addView(block)
        }
    }
    /** Long server production over SSE with live progress; the screen refreshes only if the user is still there. */
    private fun produce(path:String,message:String,done:()->Unit) {
        val text=TextView(this).apply {text=message;textSize=15f;setPadding(dp(24),dp(16),dp(24),dp(8))}
        val bar=ProgressBar(this,null,android.R.attr.progressBarStyleHorizontal).apply {max=100;isIndeterminate=true}
        val box=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL;addView(text);addView(bar,LinearLayout.LayoutParams(-1,-2).apply {setMargins(dp(24),0,dp(24),dp(16))})}
        val dialog=AlertDialog.Builder(this).setTitle("Produktion").setView(box).setCancelable(false).setNegativeButton("Ausblenden",null).create();dialog.show()
        val revision=generation
        worker.execute {
            val result=runCatching {
                api.stream(path,"POST",null) {kind,payload->
                    val event=runCatching {JSONObject(payload)}.getOrNull()
                    if(kind=="progress")runOnUiThread {if(isDestroyed || event==null)return@runOnUiThread;text.text=event.optString("message").ifBlank {message};val raw=event.optDouble("progress",-1.0);val pct=if(raw<=1.0)(raw*100).toInt() else raw.toInt();if(pct in 0..100){bar.isIndeterminate=false;bar.progress=pct}}
                    else if(kind=="error")throw Exception(event?.optString("error")?.ifBlank {null} ?: payload)
                }
            }
            runOnUiThread {
                if(isDestroyed)return@runOnUiThread
                dialog.dismiss()
                result.onSuccess {Toast.makeText(this,"Produktion abgeschlossen",Toast.LENGTH_SHORT).show()}.onFailure {fail(it)}
                if(generation==revision)done()
            }
        }
    }
    private fun field(hint:String,value:String="",lines:Int=1)=EditText(this).apply {this.hint=hint;setText(value);imeOptions=imeOptions or android.view.inputmethod.EditorInfo.IME_FLAG_NO_EXTRACT_UI or android.view.inputmethod.EditorInfo.IME_FLAG_NO_FULLSCREEN;if(lines>1){minLines=lines;gravity=Gravity.TOP;inputType=android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE or android.text.InputType.TYPE_TEXT_FLAG_CAP_SENTENCES} else inputType=android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_FLAG_CAP_SENTENCES}
    private fun form(vararg views:View)=ScrollView(this).apply {addView(LinearLayout(this@NativeActivity).apply {orientation=LinearLayout.VERTICAL;setPadding(dp(20),dp(8),dp(20),dp(4));views.forEach {addView(it)}})}
    private fun choice(title:String,options:List<String>,selected:Int):Pair<View,()->Int> {
        val group=RadioGroup(this).apply {orientation=RadioGroup.HORIZONTAL}
        options.forEachIndexed {i,o->group.addView(RadioButton(this).apply {text=o;id=i+1;isChecked=i==selected})}
        val box=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL;addView(caption(title));addView(group)}
        return box to {(group.checkedRadioButtonId-1).coerceAtLeast(0)}
    }
    private fun podcastForm(existing:JSONObject?,done:(JSONObject)->Unit) {
        val topic=field("Thema oder Titel",existing?.optString("topic").orEmpty())
        val language=field("Sprache (z. B. de)",existing?.optString("language") ?: "de")
        val formats=listOf("dialog","monolog");val durations=listOf("short","medium","long")
        val (formatView,format)=choice("Format",listOf("Dialog","Monolog"),formats.indexOf(existing?.optString("format") ?: "dialog").coerceAtLeast(0))
        val (durationView,duration)=choice("Länge",listOf("Kurz","Mittel","Lang"),durations.indexOf(existing?.optString("duration") ?: "medium").coerceAtLeast(0))
        AlertDialog.Builder(this).setTitle(if(existing==null)"Neue Episode" else "Episode bearbeiten").setView(form(topic,language,formatView,durationView)).setNegativeButton("Abbrechen",null).setPositiveButton(if(existing==null)"Anlegen" else "Speichern"){_,_->
            val body=JSONObject().put("topic",topic.text.toString().trim()).put("language",language.text.toString().trim().ifBlank {"de"}).put("format",formats[format()]).put("duration",durations[duration()])
            if(existing==null)mutate("/api/podcasts","POST",body){created->done(created as JSONObject)} else mutate("/api/podcasts/${existing.getString("id")}","PATCH",body){done(it as JSONObject)}
        }.show()
    }
    private fun assignHosts(id:String,current:JSONArray,done:()->Unit) {
        val dialog=busy("Moderatoren werden geladen …")
        worker.execute {
            val result=runCatching {array(api.request("/api/hosts"),"hosts")}
            runOnUiThread {
                if(isDestroyed)return@runOnUiThread
                dialog.dismiss()
                result.onSuccess {hosts->
                    if(hosts.length()==0){AlertDialog.Builder(this).setTitle("Keine Moderatoren").setMessage("Lege zuerst unter „Moderatoren“ Stimmen an.").setPositiveButton("Zu den Moderatoren"){_,_->pages.addLast(destination ?: {home()});hostsScreen()}.setNegativeButton("Schließen",null).show();return@onSuccess}
                    val chosen=(0 until current.length()).map {current.getJSONObject(it).optString("id")}.toMutableSet()
                    val names=Array(hosts.length()) {i->val h=hosts.getJSONObject(i);"${h.optString("name")} · ${if(h.optString("role")=="expert")"Experte" else "Moderation"}"}
                    val checked=BooleanArray(hosts.length()) {i->hosts.getJSONObject(i).optString("id") in chosen}
                    AlertDialog.Builder(this).setTitle("Moderatoren zuordnen").setMultiChoiceItems(names,checked){_,i,on->val hid=hosts.getJSONObject(i).optString("id");if(on)chosen.add(hid) else chosen.remove(hid)}.setNegativeButton("Abbrechen",null).setPositiveButton("Übernehmen"){_,_->
                        val ids=JSONArray();(0 until hosts.length()).map {hosts.getJSONObject(it).optString("id")}.filter {it in chosen}.forEach {ids.put(it)}
                        mutate("/api/podcasts/$id","PATCH",JSONObject().put("host_ids",ids)){done()}
                    }.show()
                }.onFailure {fail(it)}
            }
        }
    }
    private fun textSourceDialog(id:String,done:()->Unit) {
        val title=field("Titel (optional)");val body=field("Text der Quelle",lines=6)
        AlertDialog.Builder(this).setTitle("Text als Quelle").setView(form(title,body)).setNegativeButton("Abbrechen",null).setPositiveButton("Hinzufügen"){_,_->mutate("/api/podcasts/$id/sources/text","POST",JSONObject().put("content",body.text.toString()).put("title",title.text.toString().trim())){done()}}.show()
    }
    private fun urlSourceDialog(id:String,done:()->Unit) {
        val url=field("https://…").apply {inputType=android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_URI}
        AlertDialog.Builder(this).setTitle("Link als Quelle").setView(form(url)).setNegativeButton("Abbrechen",null).setPositiveButton("Hinzufügen"){_,_->mutate("/api/podcasts/$id/sources/url","POST",JSONObject().put("url",url.text.toString().trim())){done()}}.show()
    }
    private fun uploadSource(id:String,uri:android.net.Uri,done:()->Unit) {
        val dialog=busy("Datei wird hochgeladen und verarbeitet …")
        worker.execute {
            val result=runCatching {val (file,mime,bytes)=readDocument(uri,60*1024*1024);api.upload("/api/podcasts/$id/sources/upload",emptyMap(),"file",file,mime,bytes)}
            runOnUiThread {if(isDestroyed)return@runOnUiThread;dialog.dismiss();result.onSuccess {done()}.onFailure {fail(it)}}
        }
    }
    private fun editSegment(id:String,seg:JSONObject,done:()->Unit) {
        val segId=seg.optString("id");val type=seg.optString("type","speech")
        val editor=if(type=="pause")field("Dauer in ms",seg.optInt("duration_ms",600).toString()).apply {inputType=android.text.InputType.TYPE_CLASS_NUMBER} else field("Text",seg.optString("text"),lines=5)
        AlertDialog.Builder(this).setTitle(if(type=="pause")"Pause" else seg.optString("speaker","Segment")).setView(form(editor)).setNegativeButton("Abbrechen",null)
            .setNeutralButton("Löschen"){_,_->mutate("/api/podcasts/$id/script/segments/$segId","DELETE",null){done()}}
            .setPositiveButton("Speichern"){_,_->val body=if(type=="pause")JSONObject().put("duration_ms",editor.text.toString().toIntOrNull() ?: 600) else JSONObject().put("text",editor.text.toString());mutate("/api/podcasts/$id/script/segments/$segId","PATCH",body){done()}}.show()
    }
    private fun hostsScreen() {
        destination={hostsScreen()}
        reset("Moderatoren",back=true)
        content.addView(caption("Persönlichkeiten mit Stimme, die deine Episoden sprechen. Antippen zum Bearbeiten."))
        val adders=LinearLayout(this)
        adders.addView(button("Aus Vorlage") {hostPresets()}.apply {gravity=Gravity.CENTER},LinearLayout.LayoutParams(0,-2,1f).apply {rightMargin=dp(6)})
        adders.addView(button("Eigener Host") {hostForm(null){hostsScreen()}}.apply {gravity=Gravity.CENTER},LinearLayout.LayoutParams(0,-2,1f))
        content.addView(adders,LinearLayout.LayoutParams(-1,-2).apply {topMargin=dp(6);bottomMargin=dp(12)})
        fetch("/api/hosts") {value->
            val hosts=array(value,"hosts")
            if(hosts.length()==0)content.addView(caption("Noch keine Moderatoren angelegt."))
            for(i in 0 until hosts.length()) {
                val h=hosts.getJSONObject(i);val hid=h.optString("id")
                val row=LinearLayout(this).apply {gravity=Gravity.CENTER_VERTICAL;setPadding(dp(12),dp(10),dp(8),dp(10));background=NativeDesign.touch(this@NativeActivity,Color.TRANSPARENT,12);isClickable=true;isFocusable=true;isLongClickable=true;contentDescription="Moderator ${h.optString("name")}"
                    setOnClickListener {hostForm(h){hostsScreen()}}
                    setOnLongClickListener {AlertDialog.Builder(this@NativeActivity).setTitle(h.optString("name")).setMessage("Diesen Moderator löschen?").setNegativeButton("Abbrechen",null).setPositiveButton("Löschen"){_,_->mutate("/api/hosts/$hid","DELETE",null){hostsScreen()}}.show();true}}
                row.addView(NativeIcon(this,"account",if(h.optBoolean("voice_available",true))NativeDesign.accent else NativeDesign.muted),LinearLayout.LayoutParams(dp(36),dp(42)))
                val words=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL;setPadding(dp(8),0,0,0)}
                words.addView(NativeDesign.text(this,h.optString("name"),16,bold=true))
                words.addView(caption("${if(h.optString("role")=="expert")"Experte" else "Moderation"} · ${h.optString("voice_id").takeIf {it.isNotBlank() && it!="null"} ?: "ohne Stimme"}${if(h.optBoolean("voice_available",true))"" else " (nicht verfügbar)"}").apply {setPadding(0,dp(3),0,0);textSize=12f})
                h.optString("tagline").takeIf {it.isNotBlank() && it!="null"}?.let {words.addView(caption(it).apply {maxLines=2;ellipsize=TextUtils.TruncateAt.END;textSize=12f})}
                row.addView(words,LinearLayout.LayoutParams(0,-2,1f));row.addView(NativeIcon(this,"next"),LinearLayout.LayoutParams(dp(36),dp(42)))
                content.addView(row)
            }
        }
    }
    private fun hostPresets() {
        val dialog=busy("Vorlagen werden geladen …")
        worker.execute {
            val result=runCatching {api.request("/api/hosts/presets") as JSONObject}
            runOnUiThread {
                if(isDestroyed)return@runOnUiThread
                dialog.dismiss()
                result.onSuccess {data->
                    val presets=data.optJSONArray("presets") ?: JSONArray()
                    val labels=Array(presets.length()) {i->val p=presets.getJSONObject(i);"${p.optString("name")}${if(p.optBoolean("already_added"))" ✓" else ""}\n${p.optString("tagline")} · ${p.optString("voice")}${if(p.optBoolean("voice_available",true))"" else " (Stimme fehlt)"}"}
                    AlertDialog.Builder(this).setTitle("Vorlage wählen").setItems(labels){_,i->mutate("/api/hosts/presets/${presets.getJSONObject(i).optString("id")}","POST",JSONObject()){hostsScreen()}}.setNegativeButton("Abbrechen",null).show()
                }.onFailure {fail(it)}
            }
        }
    }
    private fun hostForm(existing:JSONObject?,done:()->Unit) {
        val name=field("Name",existing?.optString("name").orEmpty())
        val tagline=field("Kurzbeschreibung",existing?.optString("tagline")?.takeIf {it!="null"}.orEmpty())
        val personality=field("Persönlichkeit",existing?.optString("personality")?.takeIf {it!="null"}.orEmpty(),lines=3)
        val style=field("Sprechstil",existing?.optString("speaking_style")?.takeIf {it!="null"}.orEmpty(),lines=2)
        val (roleView,role)=choice("Rolle",listOf("Moderation","Experte"),if(existing?.optString("role")=="expert")1 else 0)
        var voiceId=existing?.optString("voice_id")?.takeIf {it.isNotBlank() && it!="null"}.orEmpty()
        val voiceButton=button("Stimme · ${voiceId.ifBlank {"keine"}}") {}
        voiceButton.setOnClickListener {chooseVoice(voiceId) {id,n->voiceId=id;voiceButton.text="Stimme · $n"}}
        AlertDialog.Builder(this).setTitle(if(existing==null)"Eigener Host" else "Host bearbeiten").setView(form(name,tagline,personality,style,roleView,voiceButton)).setNegativeButton("Abbrechen",null).setPositiveButton("Speichern"){_,_->
            val body=JSONObject().put("name",name.text.toString().trim()).put("tagline",tagline.text.toString().trim()).put("personality",personality.text.toString().trim()).put("speaking_style",style.text.toString().trim()).put("role",if(role()==1)"expert" else "host").put("voice_id",voiceId.ifBlank {JSONObject.NULL})
            if(existing==null)mutate("/api/hosts","POST",body){done()} else mutate("/api/hosts/${existing.getString("id")}","PATCH",body){done()}
        }.show()
    }
    private fun importBook(uri:android.net.Uri) {
        var suggested="";contentResolver.query(uri,arrayOf(android.provider.OpenableColumns.DISPLAY_NAME),null,null,null)?.use {if(it.moveToFirst())suggested=(it.getString(0) ?: "").substringBeforeLast('.')}
        val title=field("Titel",suggested);val author=field("Autor (optional)")
        AlertDialog.Builder(this).setTitle("Buch importieren").setMessage("PDF, EPUB, DOCX oder TXT werden in Kapitel und Absätze gegliedert.").setView(form(title,author)).setNegativeButton("Abbrechen",null).setPositiveButton("Importieren"){_,_->
            val dialog=busy("Buch wird hochgeladen und gegliedert …")
            worker.execute {
                val result=runCatching {val (file,mime,bytes)=readDocument(uri,120*1024*1024);api.upload("/api/audiobooks",mapOf("title" to title.text.toString().trim(),"author" to author.text.toString().trim()),"file",file,mime,bytes,readTimeout=1800000) as JSONObject}
                runOnUiThread {if(isDestroyed)return@runOnUiThread;dialog.dismiss();result.onSuccess {book->pages.addLast {area=0;home()};detail(0,book,book.optString("title",title.text.toString()))}.onFailure {fail(it)}}
            }
        }.show()
    }
    private fun bookMenu(id:String,item:JSONObject,name:String,data:JSONObject) {
        val kind=0;val refresh={detail(kind,item,name)}
        val hidden=data.optBoolean("is_hidden")
        val running=data.optJSONObject("generation")?.optString("status")=="running"
        val options=arrayListOf("Titel und Autor bearbeiten","Erzählstimme auswählen",if(running)"Vertonung abbrechen" else "Vertonung einreihen","In Sammlung aufnehmen","Als M4B exportieren","Als MP3 exportieren",if(hidden)"Wieder anzeigen" else "Verstecken","Aktuelles Kapitel für unterwegs laden","Cover ändern","Buch löschen")
        AlertDialog.Builder(this).setTitle(name).setItems(options.toTypedArray()){_,which->when(which){
            0->editBook(data)
            1->chooseVoice(data.optString("voice_id").takeUnless {it=="null"}.orEmpty()) {voice,_->mutate("/api/audiobooks/$id","PATCH",JSONObject().put("voice_id",voice)){refresh()}}
            2->if(running)mutate("/api/audiobooks/$id/generate/cancel","POST",JSONObject()){refresh()} else chooseVoice {voice,voiceName->
                AlertDialog.Builder(this).setTitle("Buch vertonen").setMessage("$name mit $voiceName in die Warteschlange stellen? Die Erzeugung läuft auf dem Server, du kannst die App schließen.").setNegativeButton("Abbrechen",null).setNeutralButton("Nur außerhalb der Geschäftszeiten"){_,_->mutate("/api/audiobooks/queue","POST",JSONObject().put("book_id",id).put("voice_id",voice).put("priority",8)){Toast.makeText(this,"Eingereiht für die Nebenzeiten",Toast.LENGTH_SHORT).show();refresh()}}.setPositiveButton("Einreihen"){_,_->mutate("/api/audiobooks/queue","POST",JSONObject().put("book_id",id).put("voice_id",voice).put("priority",5)){Toast.makeText(this,"In die Warteschlange gestellt",Toast.LENGTH_SHORT).show();refresh()}}.show()}
            3->collectionDialog(id,item.optJSONArray("collectionIds") ?: JSONArray()){refresh()}
            4->exportBook(id,name,"m4b")
            5->exportBook(id,name,"mp3")
            6->mutate("/api/audiobooks/$id","PATCH",JSONObject().put("is_hidden",!hidden)){refresh()}
            7->{val voice=data.optString("voice_id").takeUnless {it=="null"}.orEmpty();if(voice.isBlank())Toast.makeText(this,"Bitte zuerst eine Erzählstimme auswählen.",Toast.LENGTH_LONG).show() else prefetchChapter(id,name,voice,data.optJSONObject("progress")?.optInt("chapterIndex") ?: 0)}
            8->pick("image/*") {uri->val dialog=busy("Cover wird hochgeladen …");worker.execute {val result=runCatching {val (file,mime,bytes)=readDocument(uri,10*1024*1024);api.upload("/api/audiobooks/$id/cover",emptyMap(),"cover",file,mime,bytes)};runOnUiThread {if(isDestroyed)return@runOnUiThread;dialog.dismiss();result.onSuccess {coverCache.remove(id);refresh()}.onFailure {fail(it)}}}}
            else->AlertDialog.Builder(this).setTitle("Buch löschen").setMessage("„$name“ mit Text, Audio und Fortschritt endgültig löschen?").setNegativeButton("Abbrechen",null).setPositiveButton("Löschen"){_,_->mutate("/api/audiobooks/$id","DELETE",null){pages.clear();area=0;home()}}.show()
        }}.show()
    }
    private fun collectionDialog(bookId:String,memberships:JSONArray,done:()->Unit) {
        val member=(0 until memberships.length()).map {memberships.optString(it)}.toSet()
        fetchDialog("/api/audiobooks/collections","Sammlungen werden geladen …") {value->
            val collections=array(value,"collections")
            val labels=(0 until collections.length()).map {val c=collections.getJSONObject(it);(if(c.optString("id") in member)"✓ " else "")+c.optString("name")}+"Neue Sammlung …"
            AlertDialog.Builder(this).setTitle("Sammlung").setItems(labels.toTypedArray()){_,i->
                if(i==collections.length()){val nameField=field("Name der Sammlung");AlertDialog.Builder(this).setTitle("Neue Sammlung").setView(form(nameField)).setNegativeButton("Abbrechen",null).setPositiveButton("Anlegen"){_,_->mutate("/api/audiobooks/collections","POST",JSONObject().put("name",nameField.text.toString().trim())){created->mutate("/api/audiobooks/collections/${(created as JSONObject).optString("id")}/books","POST",JSONObject().put("bookId",bookId)){done()}}}.show()}
                else {val cid=collections.getJSONObject(i).optString("id");if(cid in member)mutate("/api/audiobooks/collections/$cid/books/$bookId","DELETE",null){done()} else mutate("/api/audiobooks/collections/$cid/books","POST",JSONObject().put("bookId",bookId)){done()}}
            }.setNegativeButton("Schließen",null).show()
        }
    }
    /** Loads one JSON resource behind a blocking dialog and hands it to the caller on the UI thread. */
    private fun fetchDialog(path:String,message:String,render:(Any)->Unit) {
        val dialog=busy(message)
        worker.execute {val result=runCatching {api.request(path)};runOnUiThread {if(isDestroyed)return@runOnUiThread;dialog.dismiss();result.onSuccess(render).onFailure {fail(it)}}}
    }
    /** Cache-only export: start, poll the job, then hand the file to the system download manager. */
    private fun exportBook(id:String,name:String,format:String) {
        val dialog=busy("Export als ${format.uppercase()} läuft …")
        worker.execute {
            val result=runCatching {
                val status=api.request("/api/audiobooks/$id/export/status?format=$format") as JSONObject
                if(!(status.optBoolean("ready") && status.optJSONObject("job")?.optString("status")!="running"))api.request("/api/audiobooks/$id/export","POST",JSONObject().put("format",format))
                var waited=0
                while(true){
                    val current=api.request("/api/audiobooks/$id/export/status?format=$format") as JSONObject
                    val job=current.optJSONObject("job")
                    if(job!=null && job.optString("status")=="running"){runOnUiThread {dialog.setMessage("Export als ${format.uppercase()} läuft … Kapitel ${job.optInt("done")} von ${job.optInt("total")}")}}
                    else if(job!=null && job.optString("error").isNotBlank())throw Exception(job.optString("error"))
                    else if(current.optBoolean("ready"))break
                    else if(job==null)throw Exception("Der Export wurde nicht gestartet.")
                    Thread.sleep(3000);waited+=3;if(waited>3600)throw Exception("Der Export dauert zu lange.")
                }
            }
            runOnUiThread {if(isDestroyed)return@runOnUiThread;dialog.dismiss();result.onSuccess {saveDownload("/api/audiobooks/$id/export/download?format=$format","${name.replace(Regex("[^\\p{L}\\p{N} _-]"),"_").take(80)}.$format")}.onFailure {fail(it)}}
        }
    }
    private fun bookmarks(id:String,name:String,voice:String,into:LinearLayout) {
        fetch("/api/audiobooks/$id/bookmarks",into) {value->
            val marks=array(value,"bookmarks")
            if(marks.length()==0){into.addView(caption("Noch keine Lesezeichen. Halte im Leser einen Absatz gedrückt."));return@fetch}
            for(i in 0 until marks.length()) {
                val m=marks.getJSONObject(i);val chapter=m.optInt("chapterIndex");val segment=m.optInt("segmentIndex")
                val row=LinearLayout(this).apply {gravity=Gravity.CENTER_VERTICAL;setPadding(dp(12),dp(10),dp(8),dp(10));background=NativeDesign.touch(this@NativeActivity,Color.TRANSPARENT,12);isClickable=true;isFocusable=true;isLongClickable=true;contentDescription="Lesezeichen Kapitel ${chapter+1}, Absatz ${segment+1}"
                    setOnClickListener {pages.addLast(destination ?: {home()});reader(id,chapter,name,voice)}
                    setOnLongClickListener {AlertDialog.Builder(this@NativeActivity).setTitle("Lesezeichen").setItems(arrayOf("Ab hier anhören","Löschen")){_,which->if(which==0)listen(id,name,voice,chapter,segment) else mutate("/api/audiobooks/$id/bookmarks/${m.optString("id")}","DELETE",null){destination?.invoke()}}.show();true}}
                val words=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL}
                words.addView(NativeDesign.text(this,"Kapitel ${chapter+1} · Absatz ${segment+1}",15))
                m.optString("note").takeIf {it.isNotBlank() && it!="null"}?.let {words.addView(caption(it).apply {textSize=12f})}
                row.addView(words,LinearLayout.LayoutParams(0,-2,1f));row.addView(NativeIcon(this,"next"),LinearLayout.LayoutParams(dp(36),dp(42)));into.addView(row)
            }
        }
    }
    private fun hoerspielDetail(id:String,data:JSONObject,item:JSONObject,name:String) {
        val refresh={detail(2,item,name)}
        val status=data.optString("status");val stage=data.optInt("stage")
        content.addView(caption("${when(status){"completed"->"Fertiggestellt";"draft"->"Entwurf";"running"->"In Produktion";"failed"->"Fehlgeschlagen";else->status}} · Stufe $stage · ${data.optInt("chapter_count",(data.optJSONArray("chapters") ?: JSONArray()).length())} Kapitel · ${data.optInt("cue_count",(data.optJSONArray("cues") ?: JSONArray()).length())} Erzählpassagen"))
        data.optJSONObject("binding")?.let {b->content.addView(caption("Plex: ${b.optString("title")} (${b.optInt("year")}) · ${b.optInt("seasons")} Staffeln · ${b.optInt("episodes")} Episoden"))}
        val report=data.optJSONObject("quality_report")
        if(report!=null)content.addView(caption("Qualität: ${if(report.optBoolean("release_ready"))"freigegeben" else "blockiert"} · ${report.optInt("passed_checks")}/${report.optInt("total_checks")} Kriterien · ${report.optString("summary")}").apply {if(!report.optBoolean("release_ready"))setTextColor(Color.rgb(255,204,150))})
        val artifacts=data.optJSONArray("artifacts") ?: JSONArray()
        for(i in 0 until artifacts.length()) {
            val artifact=artifacts.getJSONObject(i);val file=artifact.optString("filename");val minutes=(artifact.optJSONObject("audio")?.optLong("duration_ms") ?: 0L)/60000
            content.addView(button("Anhören · $file${if(minutes>0)" · $minutes Min" else ""}") {NativePlaybackService.play(this,NavigationPolicy.ORIGIN+"/api/hoerspiele/artifacts/${artifact.getString("id")}/content",name,"Hörspiel");playerScreen()}.apply {background=NativeDesign.touch(this@NativeActivity,NativeDesign.accentPanel);gravity=Gravity.CENTER},LinearLayout.LayoutParams(-1,-2).apply {bottomMargin=dp(8)})
            action("Herunterladen · ${artifact.optLong("bytes")/1048576} MB") {saveDownload("/api/hoerspiele/artifacts/${artifact.getString("id")}/content",file)}
        }
        headerAction("more","Projekt verwalten") {
            AlertDialog.Builder(this).setTitle(name).setItems(arrayOf("Produktion starten","Stand aktualisieren","Projekt löschen")){_,which->when(which){
                0->AlertDialog.Builder(this).setTitle("Produktion starten").setMessage("Analyse, Zuordnung, Erzählpassagen und Rendering laufen auf dem Server und dauern je nach Umfang Stunden.").setNegativeButton("Abbrechen",null).setPositiveButton("Starten"){_,_->mutate("/api/hoerspiele/projects/$id/pipeline-runs","POST",JSONObject()){Toast.makeText(this,"Produktion gestartet",Toast.LENGTH_SHORT).show();refresh()}}.show()
                1->refresh()
                else->AlertDialog.Builder(this).setTitle("Projekt löschen").setMessage("„$name“ mit Zuordnung, Passagen und Artefakten löschen?").setNegativeButton("Abbrechen",null).setPositiveButton("Löschen"){_,_->mutate("/api/hoerspiele/projects/$id","DELETE",null){pages.clear();area=2;home()}}.show()
            }}.show()
        }
        content.addView(label("Produktionsläufe",22).apply {setPadding(0,dp(20),0,dp(6))})
        val runsBox=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL};content.addView(runsBox)
        fetch("/api/hoerspiele/pipeline-runs?project_id=$id&limit=5",runsBox) {value->
            val runs=if(value is JSONArray)value else JSONArray()
            if(runs.length()==0)runsBox.addView(caption("Noch kein Lauf."))
            for(i in 0 until runs.length()) {
                val run=runs.getJSONObject(i)
                runsBox.addView(NativeDesign.text(this,"${when(run.optString("status")){"queued"->"Wartet";"running"->"Läuft";"completed"->"Abgeschlossen";"failed"->"Fehlgeschlagen";else->run.optString("status")}} · ${run.optString("stage")} · ${run.optInt("completed_units")}/${run.optInt("total_units")}",15).apply {setPadding(0,dp(6),0,0)})
                runsBox.addView(caption("${run.optString("message")}${run.optString("error").takeIf {it.isNotBlank() && it!="null"}?.let {" · $it"} ?: ""} · ${run.optString("updated_at").take(16).replace('T',' ')}").apply {textSize=12f})
            }
        }
        val mapping=data.optJSONArray("mapping") ?: JSONArray()
        if(mapping.length()>0) {
            content.addView(label("Zuordnung",22).apply {setPadding(0,dp(20),0,dp(6))})
            for(i in 0 until mapping.length()) {val m=mapping.getJSONObject(i);content.addView(caption("${m.optString("chapter_title")} → S${m.optInt("season")}E${m.optInt("episode")} ${m.optString("episode_title")} · ${(m.optDouble("confidence",0.0)*100).toInt()} %${if(m.optString("review_state")=="needs_review")" · prüfen" else ""}"))}
        }
        val chapters=data.optJSONArray("chapters") ?: JSONArray()
        val cues=data.optJSONArray("cues") ?: JSONArray()
        if(cues.length()>0) {
            content.addView(label("Erzählpassagen",22).apply {setPadding(0,dp(20),0,dp(6))})
            val box=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL;visibility=View.GONE}
            action("${cues.length()} Passagen anzeigen") {box.visibility=if(box.visibility==View.GONE)View.VISIBLE else View.GONE}
            content.addView(box)
            val sampleRate=data.optJSONObject("timeline")?.optJSONObject("timebase")?.optInt("sample_rate",48000) ?: 48000
            val playable=artifacts.takeIf {it.length()>0}?.getJSONObject(0)
            if(playable!=null)box.addView(caption("Passage antippen, um die Stelle im Hörspiel anzuhören."))
            for(i in 0 until cues.length()) {val cue=cues.getJSONObject(i);val at=if(cue.isNull("timeline_start_sample"))-1L else cue.optLong("timeline_start_sample")*1000/sampleRate
                val block=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL;setPadding(dp(10),dp(8),dp(10),dp(8));background=NativeDesign.touch(this@NativeActivity,Color.TRANSPARENT,12);isClickable=playable!=null && at>=0;isFocusable=true
                    if(playable!=null && at>=0)setOnClickListener {NativePlaybackService.playAt(this@NativeActivity,NavigationPolicy.ORIGIN+"/api/hoerspiele/artifacts/${playable.getString("id")}/content",name,"Hörspiel",at);playerScreen()}}
                block.addView(caption("${cue.optString("chapter_title")}${if(at>=0)" · ${at/60000}:${((at/1000)%60).toString().padStart(2,'0')}" else ""} · ${cue.optString("narrative_purpose")}").apply {textSize=12f})
                block.addView(NativeDesign.text(this,cue.optString("text"),15).apply {setLineSpacing(dp(3).toFloat(),1f)});box.addView(block)}
        } else if(chapters.length()>0) {
            content.addView(label("Kapitel",22).apply {setPadding(0,dp(20),0,dp(6))})
            for(i in 0 until chapters.length()) {val c=chapters.getJSONObject(i);content.addView(NativeDesign.text(this,c.optString("title"),15).apply {setPadding(0,dp(6),0,0)});content.addView(caption(c.optString("preview")).apply {maxLines=3;ellipsize=TextUtils.TruncateAt.END;textSize=12f})}
        }
    }
    private fun providerForm(existing:JSONObject?,done:()->Unit) {
        val name=field("Name",existing?.optString("name").orEmpty())
        val url=field("API-Basis-URL (OpenAI-kompatibel)",existing?.optString("base_url").orEmpty()).apply {inputType=android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_URI}
        val model=field("Modell",existing?.optString("model").orEmpty()).apply {inputType=android.text.InputType.TYPE_CLASS_TEXT}
        val key=field(if(existing==null)"API-Schlüssel" else "API-Schlüssel (leer = unverändert)").apply {inputType=android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD}
        AlertDialog.Builder(this).setTitle(if(existing==null)"Anbieter hinzufügen" else "Anbieter bearbeiten").setView(form(name,url,model,key)).setNegativeButton("Abbrechen",null).setPositiveButton("Speichern"){_,_->
            val body=JSONObject().put("name",name.text.toString().trim()).put("base_url",url.text.toString().trim()).put("model",model.text.toString().trim())
            if(key.text.isNotBlank())body.put("api_key",key.text.toString().trim())
            if(existing==null)mutate("/api/llm/providers","POST",body){done()} else mutate("/api/llm/providers/${existing.optString("id")}","PATCH",body){done()}
        }.show()
    }
    /** Loads every segment of a chapter into the playback cache so it plays without a connection. */
    private fun prefetchChapter(id:String,name:String,voice:String,chapter:Int) {
        val dialog=busy("Kapitel wird geladen …")
        worker.execute {
            val result=runCatching {
                val (tracks,_)=chapterTracks(id,name,voice,chapter)
                var done=0
                for(track in tracks){if(NativePlaybackService.download(this,api,track))done++;val n=done;runOnUiThread {dialog.setMessage("Kapitel wird geladen … $n von ${tracks.size} Abschnitten")}}
                done to tracks.size
            }
            runOnUiThread {if(isDestroyed)return@runOnUiThread;dialog.dismiss();result.onSuccess {(done,total)->if(done==total && total>0){val set=HashSet(prefs.getStringSet("offline-chapters",emptySet()) ?: emptySet());set.add("$id:$chapter");prefs.edit().putStringSet("offline-chapters",set).apply()};Toast.makeText(this,if(done==total)"Kapitel ${chapter+1} ist unterwegs verfügbar ($total Abschnitte)" else "$done von $total Abschnitten geladen",Toast.LENGTH_LONG).show();destination?.invoke()}.onFailure {fail(it)}}
        }
    }
    private fun shareClip(clip:File,title:String) {
        val uri=androidx.core.content.FileProvider.getUriForFile(this,"ch.zwaetschge.vocarium.files",clip)
        val send=android.content.Intent(android.content.Intent.ACTION_SEND).setType(if(clip.extension=="mp3")"audio/mpeg" else "audio/wav").putExtra(android.content.Intent.EXTRA_STREAM,uri).putExtra(android.content.Intent.EXTRA_SUBJECT,title).addFlags(android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION)
        startActivity(android.content.Intent.createChooser(send,"Clip teilen"))
    }
    private fun bookSearch(id:String,name:String,voice:String) {
        val query=field("Wort oder Satz")
        AlertDialog.Builder(this).setTitle("Im Buch suchen").setView(form(query)).setNegativeButton("Abbrechen",null).setPositiveButton("Suchen"){_,_->
            val q=query.text.toString().trim();if(q.isEmpty())return@setPositiveButton
            fetchDialog("/api/audiobooks/$id/search?q=${android.net.Uri.encode(q)}","Suche läuft …") {value->
                val d=value as JSONObject;val results=d.optJSONArray("results") ?: JSONArray()
                if(!d.optBoolean("ready",true)){Toast.makeText(this,"Die Suche ist für dieses Buch noch nicht vorbereitet.",Toast.LENGTH_LONG).show();return@fetchDialog}
                if(results.length()==0){Toast.makeText(this,"Keine Treffer für „$q“.",Toast.LENGTH_SHORT).show();return@fetchDialog}
                val labels=Array(results.length()) {i->val r=results.getJSONObject(i);"Kapitel ${r.optInt("chapterIndex")+1}: ${r.optString("text").take(120)}"}
                AlertDialog.Builder(this).setTitle("${results.length()} Treffer für „$q“").setItems(labels){_,i->val r=results.getJSONObject(i);pages.addLast(destination ?: {home()});reader(id,r.optInt("chapterIndex"),name,voice,r.optInt("index"))}.setNegativeButton("Schließen",null).show()
            }
        }.show()
    }
    private fun bookmarkDialog(bookId:String,chapter:Int,segment:Int,context:String) {
        val note=field("Notiz (optional)")
        AlertDialog.Builder(this).setTitle("Lesezeichen · Kapitel ${chapter+1}, Absatz ${segment+1}").setMessage(context.take(140)).setView(form(note)).setNegativeButton("Abbrechen",null).setPositiveButton("Speichern"){_,_->mutate("/api/audiobooks/$bookId/bookmarks","POST",JSONObject().put("chapterIndex",chapter).put("segmentIndex",segment).put("note",note.text.toString().trim())){Toast.makeText(this,"Lesezeichen gespeichert",Toast.LENGTH_SHORT).show()}}.show()
    }
    private fun playerChapters(track:Track) {
        fetchDialog("/api/audiobooks/${track.bookId}","Kapitel werden geladen …") {value->
            val data=value as JSONObject;val chapters=data.optJSONArray("chapters") ?: JSONArray()
            val titles=Array(chapters.length()) {i->val c=chapters.getJSONObject(i);"${(i+1).toString().padStart(2,'0')}  ${c.optString("title","Kapitel ${i+1}")}${if(c.optInt("index",i)==track.chapter)"  ●" else ""}"}
            AlertDialog.Builder(this).setTitle("Kapitel").setItems(titles){_,i->listen(track.bookId,track.title,track.voice.ifBlank {data.optString("voice_id")},chapters.getJSONObject(i).optInt("index",i),0)}.setNegativeButton("Schließen",null).show()
        }
    }
    private fun editBook(data:JSONObject) {
        val id=data.getString("id")
        val draftKey="book-draft-$id"
        val saved=runCatching {JSONObject(prefs.getString(draftKey,null) ?: "{}")} .getOrDefault(JSONObject())
        val fields=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL;setPadding(dp(20),dp(12),dp(20),dp(12))}
        val bookTitle=EditText(this).apply {hint="Titel";imeOptions=imeOptions or android.view.inputmethod.EditorInfo.IME_FLAG_NO_EXTRACT_UI or android.view.inputmethod.EditorInfo.IME_FLAG_NO_FULLSCREEN;setText(saved.optString("title",data.optString("title")));inputType=android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_FLAG_CAP_SENTENCES};fields.addView(bookTitle)
        val author=EditText(this).apply {hint="Autor";imeOptions=imeOptions or android.view.inputmethod.EditorInfo.IME_FLAG_NO_EXTRACT_UI or android.view.inputmethod.EditorInfo.IME_FLAG_NO_FULLSCREEN;setText(saved.optString("author",data.optString("author")));inputType=android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_FLAG_CAP_WORDS};fields.addView(author)
        val errorView=TextView(this).apply {textSize=14f;setPadding(0,dp(12),0,0)};fields.addView(errorView)
        fun draft()=JSONObject().put("title",bookTitle.text.toString()).put("author",author.text.toString())
        val watcher=object:TextWatcher {override fun beforeTextChanged(s:CharSequence?,start:Int,count:Int,after:Int){};override fun onTextChanged(s:CharSequence?,start:Int,before:Int,count:Int){prefs.edit().putString(draftKey,draft().toString()).apply()};override fun afterTextChanged(s:Editable?){} }
        bookTitle.addTextChangedListener(watcher);author.addTextChangedListener(watcher)
        val dialog=AlertDialog.Builder(this).setTitle("Buch bearbeiten").setView(fields).setNegativeButton("Schließen",null).setPositiveButton("Speichern",null).create()
        dialog.setOnShowListener {
            val save=dialog.getButton(AlertDialog.BUTTON_POSITIVE)
            save.setOnClickListener {
                if(bookTitle.text.isNullOrBlank()){bookTitle.error="Bitte einen Titel eingeben";return@setOnClickListener}
                val body=draft();save.isEnabled=false;errorView.text="Wird gespeichert …"
                worker.execute {
                    val result=runCatching {api.request("/api/audiobooks/$id","PATCH",body)}
                    runOnUiThread {
                        if(isDestroyed)return@runOnUiThread
                        save.isEnabled=true
                        result.onSuccess {prefs.edit().remove(draftKey).apply();dialog.dismiss();detail(0,data,body.getString("title"))}.onFailure {errorView.text="Speichern fehlgeschlagen. Dein Entwurf bleibt erhalten."}
                    }
                }
            }
        }
        dialog.show()
    }
    private fun mutate(path:String,method:String,body:JSONObject?,done:(Any)->Unit) {
        val revision=generation
        worker.execute {
            val result=runCatching {api.request(path,method,body)}
            runOnUiThread {if(!isDestroyed && generation==revision)result.onSuccess(done).onFailure {error->AlertDialog.Builder(this).setTitle("Aktion fehlgeschlagen").setMessage(error.message).setPositiveButton("OK",null).show()}}
        }
    }
    private fun chooseVoice(current:String="",selected:(String,String)->Unit) {
        fetchDialog("/api/voices","Stimmen werden geladen …") {value ->
            val raw=array(value,"voices")
            val voices=(0 until raw.length()).map {raw.getJSONObject(it)}.sortedWith(compareBy<JSONObject> {if(it.optString("engine").ifBlank {it.optString("source")}=="omnivoice")0 else 1}.thenBy {it.optString("name").lowercase()})
            val titles=Array(voices.size) {i->val v=voices[i];val id=v.optString("id").ifBlank {v.optString("voice_id")};(if(id==current)"✓ " else "")+v.optString("name")+" · "+(if(v.optString("engine").ifBlank {v.optString("source")}=="omnivoice")"OmniVoice" else "Kikiri")}
            AlertDialog.Builder(this).setTitle("Stimme auswählen").setItems(titles) {_,index ->
                val voice=voices[index];selected(voice.optString("id").ifBlank {voice.optString("voice_id")},voice.optString("name"))
            }.setNegativeButton("Abbrechen",null).show()
        }
    }
    private fun saveDownload(path:String,name:String) {
        val manager=getSystemService(android.app.DownloadManager::class.java)
        val uri=android.net.Uri.parse(NavigationPolicy.ORIGIN+path)
        val request=android.app.DownloadManager.Request(uri).setTitle(name).setNotificationVisibility(android.app.DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            .setDestinationInExternalPublicDir(android.os.Environment.DIRECTORY_DOWNLOADS,name.replace(Regex("[^\\p{L}\\p{N}._ -]"),"_"))
        android.webkit.CookieManager.getInstance().getCookie(NavigationPolicy.ORIGIN)?.let {request.addRequestHeader("Cookie",it)}
        try {manager.enqueue(request);Toast.makeText(this,"Download gestartet",Toast.LENGTH_SHORT).show()}catch(_:Exception){Toast.makeText(this,"Download konnte nicht gestartet werden",Toast.LENGTH_LONG).show()}
    }
    private fun account() {
        pages.addLast(destination ?: {home()});destination={account()};reset("Konto",back=true)
        content.addView(caption("Server: ${NavigationPolicy.ORIGIN}"))
        val who=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL};content.addView(who)
        fetch("/api/auth/me",who) {value ->
            val user=(value as JSONObject).optJSONObject("user") ?: value
            who.addView(label(user.optString("display_name").ifBlank {user.optString("username","Angemeldet")},25))
            who.addView(caption("Angemeldet als ${user.optString("username")}"))
            who.addView(button("Abmelden") {AlertDialog.Builder(this).setTitle("Abmelden").setMessage("Die Sitzung auf diesem Gerät beenden?").setNegativeButton("Abbrechen",null).setPositiveButton("Abmelden"){_,_->android.webkit.CookieManager.getInstance().removeAllCookies {runOnUiThread {pages.clear();home()}}}.show()},LinearLayout.LayoutParams(-1,-2).apply {topMargin=dp(10)})
            content.addView(label("KI-Anbieter für Podcast-Skripte",22).apply {setPadding(0,dp(24),0,dp(6))})
            content.addView(caption("Antippen zum Aktivieren, gedrückt halten zum Bearbeiten oder Löschen."))
            val box=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL};content.addView(box)
            fetch("/api/llm/providers",box) {list->
                val providers=array(list,"providers")
                if(providers.length()==0)box.addView(caption("Noch kein Anbieter hinterlegt."))
                for(i in 0 until providers.length()) {
                    val p=providers.getJSONObject(i);val pid=p.optString("id");val active=p.optBoolean("is_active")
                    val row=LinearLayout(this).apply {gravity=Gravity.CENTER_VERTICAL;setPadding(dp(12),dp(10),dp(8),dp(10));background=NativeDesign.touch(this@NativeActivity,if(active)NativeDesign.panel else Color.TRANSPARENT,12);isClickable=true;isFocusable=true;isLongClickable=true;contentDescription="Anbieter ${p.optString("name")}"
                        setOnClickListener {if(!active)mutate("/api/llm/providers/$pid/set-active","POST",JSONObject()){account().also {pages.pollLast()}}}
                        setOnLongClickListener {AlertDialog.Builder(this@NativeActivity).setTitle(p.optString("name")).setItems(arrayOf("Bearbeiten","Löschen")){_,which->if(which==0)providerForm(p){account().also {pages.pollLast()}} else mutate("/api/llm/providers/$pid","DELETE",null){account().also {pages.pollLast()}}}.show();true}}
                    val words=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL}
                    words.addView(NativeDesign.text(this,p.optString("name"),16,bold=active))
                    words.addView(caption("${p.optString("model")} · ${p.optString("base_url")}${if(active)" · aktiv" else ""}").apply {textSize=12f;maxLines=2;ellipsize=TextUtils.TruncateAt.END})
                    row.addView(words,LinearLayout.LayoutParams(0,-2,1f));box.addView(row)
                }
                box.addView(button("Anbieter hinzufügen") {providerForm(null){account().also {pages.pollLast()}}},LinearLayout.LayoutParams(-1,-2).apply {topMargin=dp(8)})
            }
        }
        content.addView(label("Hörstatistik",22).apply {setPadding(0,dp(24),0,dp(6))})
        val statsBox=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL};content.addView(statsBox)
        fetch("/api/audiobooks/stats",statsBox) {value->
            val d=value as JSONObject;val st=d.optJSONObject("stats") ?: JSONObject()
            val hours=st.optLong("totalListeningMs")/3600000.0
            statsBox.addView(caption("${String.format(java.util.Locale.GERMAN,"%.1f",hours)} Stunden gehört · ${st.optInt("sessions")} Sitzungen · ${st.optInt("segmentsPlayed")} Abschnitte · Serie: ${st.optInt("streak")} Tage"))
            statsBox.addView(caption("${st.optInt("totalBooks")} Bücher · ${st.optInt("booksStarted")} begonnen · ${st.optInt("booksCompleted")} beendet · ${st.optInt("totalBookmarks")} Lesezeichen"))
            val ach=d.optJSONArray("achievements") ?: JSONArray()
            val unlocked=(0 until ach.length()).map {ach.getJSONObject(it)}.filter {it.optBoolean("unlocked")}
            if(ach.length()>0)statsBox.addView(caption("Erfolge: ${unlocked.size} von ${ach.length()} · "+unlocked.joinToString(" ") {"${it.optString("icon")} ${it.optString("title")}"}).apply {setPadding(0,dp(6),0,0)})
        }
        content.addView(label("Aussprache-Regeln",22).apply {setPadding(0,dp(24),0,dp(6))})
        content.addView(caption("Ersetzungen nur für die Sprachausgabe, z. B. „Dr.“ → „Doktor“. Gedrückt halten zum Löschen."))
        val rulesBox=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL};content.addView(rulesBox)
        fetch("/api/audiobooks/pronunciation",rulesBox) {value->
            val rules=array(value,"rules")
            if(rules.length()==0)rulesBox.addView(caption("Noch keine Regeln."))
            for(i in 0 until rules.length()) {val r=rules.getJSONObject(i)
                rulesBox.addView(NativeDesign.text(this,"${r.optString("original")} → ${r.optString("replacement")}",15).apply {setPadding(dp(12),dp(10),dp(12),dp(10));background=NativeDesign.touch(this@NativeActivity,Color.TRANSPARENT,12);isClickable=true;isLongClickable=true;isFocusable=true
                    setOnLongClickListener {AlertDialog.Builder(this@NativeActivity).setTitle("Regel löschen").setMessage("${r.optString("original")} → ${r.optString("replacement")}").setNegativeButton("Abbrechen",null).setPositiveButton("Löschen"){_,_->mutate("/api/audiobooks/pronunciation/${r.optString("id")}","DELETE",null){account().also {pages.pollLast()}}}.show();true}})}
            rulesBox.addView(button("Regel hinzufügen") {val original=field("Geschrieben (z. B. Dr.)");val replacement=field("Gesprochen (z. B. Doktor)");AlertDialog.Builder(this).setTitle("Aussprache-Regel").setView(form(original,replacement)).setNegativeButton("Abbrechen",null).setPositiveButton("Speichern"){_,_->mutate("/api/audiobooks/pronunciation","POST",JSONObject().put("original",original.text.toString().trim()).put("replacement",replacement.text.toString().trim())){account().also {pages.pollLast()}}}.show()},LinearLayout.LayoutParams(-1,-2).apply {topMargin=dp(8)})
        }
        content.addView(label("Version",22).apply {setPadding(0,dp(24),0,dp(6))})
        content.addView(caption("Vocarium ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE}) · nativer Client"))
        action("Anmeldung öffnen") {login()}
    }
    private fun queueScreen() {
        destination={queueScreen()};reset("Warteschlange",back=true)
        content.addView(caption("Vertonungen laufen nacheinander auf dem Server. Antippen, um einen wartenden Auftrag zu entfernen."))
        fetch("/api/audiobooks/queue") {value->
            val data=value as JSONObject;val jobs=data.optJSONArray("jobs") ?: JSONArray()
            if(data.optBoolean("offHours"))content.addView(caption("Nebenzeit aktiv: auch Aufträge mit niedriger Priorität werden verarbeitet."))
            if(jobs.length()==0)content.addView(label("Keine Aufträge",22))
            for(i in 0 until jobs.length()) {
                val j=jobs.getJSONObject(i)
                val row=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL;setPadding(dp(12),dp(10),dp(12),dp(10));background=NativeDesign.touch(this@NativeActivity,Color.TRANSPARENT,12);isClickable=true;isFocusable=true
                    setOnClickListener {AlertDialog.Builder(this@NativeActivity).setTitle(j.optString("title",j.optString("book_id"))).setMessage("Auftrag aus der Warteschlange entfernen?").setNegativeButton("Abbrechen",null).setPositiveButton("Entfernen"){_,_->mutate("/api/audiobooks/queue/${j.optString("id")}","DELETE",null){queueScreen()}}.show()}}
                row.addView(NativeDesign.text(this,j.optString("title").ifBlank {j.optString("book_id")},16))
                row.addView(caption("${j.optString("status")} · ${j.optString("voice_id")} · Priorität ${j.optInt("priority")}${j.optString("error").takeIf {it.isNotBlank() && it!="null"}?.let {" · $it"} ?: ""}").apply {textSize=12f})
                content.addView(row)
            }
        }
    }
    @SuppressLint("SetJavaScriptEnabled")
    /** Sign-in runs as a full-screen overlay inside the Activity window: dialogs do not hand the IME focus to a WebView reliably. */
    private fun login() {
        if(loginOverlay!=null)return
        val view=WebView(this)
        view.settings.javaScriptEnabled=true
        view.settings.domStorageEnabled=true
        view.isFocusable=true;view.isFocusableInTouchMode=true
        view.setOnTouchListener {v,event->if(event.action==android.view.MotionEvent.ACTION_DOWN && !v.hasFocus())v.requestFocus(View.FOCUS_DOWN);false}
        val overlay=LinearLayout(this).apply {orientation=LinearLayout.VERTICAL;setBackgroundColor(surfaceColor);isClickable=true;isFocusable=true}
        val header=LinearLayout(this).apply {gravity=Gravity.CENTER_VERTICAL;setPadding(dp(8),dp(8),dp(8),dp(4))}
        header.addView(iconButton("back","Anmeldung schließen") {closeLogin()})
        header.addView(NativeDesign.text(this,"Bei Vocarium anmelden",18,bold=true).apply {setPadding(dp(8),0,0,0)},LinearLayout.LayoutParams(0,-2,1f))
        overlay.addView(header)
        overlay.addView(view,LinearLayout.LayoutParams(-1,0,1f))
        var checking=false
        view.webViewClient=object:WebViewClient() {
            override fun shouldOverrideUrlLoading(v:WebView,request:WebResourceRequest):Boolean = !NavigationPolicy.isInternal(request.url)
            override fun onPageFinished(v:WebView,url:String) {
                if(checking)return
                checking=true
                worker.execute {
                    val authenticated=runCatching {api.request("/api/auth/me")}.isSuccess
                    runOnUiThread {checking=false;if(authenticated && !isDestroyed && loginOverlay===overlay){closeLogin();destination?.invoke() ?: home()}}
                }
            }
        }
        loginOverlay=overlay
        root.addView(overlay,FrameLayout.LayoutParams(-1,-1))
        view.loadUrl("${NavigationPolicy.ORIGIN}/api/auth/me")
        view.post {view.requestFocus(View.FOCUS_DOWN)}
    }
    private fun closeLogin() {
        val overlay=loginOverlay ?: return
        loginOverlay=null
        (overlay as? android.view.ViewGroup)?.let {group->for(i in 0 until group.childCount){(group.getChildAt(i) as? WebView)?.let {it.stopLoading();it.destroy()}}}
        root.removeView(overlay)
        (getSystemService(android.view.inputmethod.InputMethodManager::class.java)).hideSoftInputFromWindow(root.windowToken,0)
    }
    override fun onDestroy() {generation++;worker.shutdownNow();coverWorker.shutdownNow();super.onDestroy()}
}
