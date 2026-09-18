package ch.zwaetschge.vocarium

import android.app.*
import android.content.*
import android.media.*
import android.net.Uri
import android.os.*
import android.webkit.CookieManager
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import android.support.v4.media.MediaMetadataCompat
import android.support.v4.media.session.MediaSessionCompat
import android.support.v4.media.session.PlaybackStateCompat
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.Executors

/** One playable unit. Book tracks carry their listening position so the server progress follows the ear. */
class Track(val url: String, val title: String, val subtitle: String = "", val bookId: String = "", val chapter: Int = 0, val segment: Int = 0, val last: Boolean = false, val voice: String = "")

/** Owns real audio playback, independently of Activities and WebViews. Plays a queue, advances, warms ahead. */
class NativePlaybackService : Service() {
    companion object {
        var current: NativePlaybackService? = null; private set
        const val PLAY="native.play"; const val PAUSE="native.pause"; const val STOP="native.stop"; const val NEXT="native.next"; const val PREVIOUS="native.previous"; const val QUEUE="native.queue"
        private var pending: List<Track>? = null
        private var pendingMore: ((Track) -> List<Track>)? = null
        private var pendingIndex = 0
        private var pendingSeekMs = 0L
        fun play(context: Context, url: String, title: String, subtitle: String = "") = playQueue(context, listOf(Track(url, title, subtitle)), 0, null)
        /** Starts (or re-uses) a single file and jumps to a position once it is ready. */
        fun playAt(context: Context, url: String, title: String, subtitle: String, ms: Long) {
            val running = current
            if (running != null && running.track?.url == url && running.ready) { running.seek(ms); running.resume(); return }
            pendingSeekMs = ms; playQueue(context, listOf(Track(url, title, subtitle)), 0, null)
        }
        /** loadMore is asked once the queue runs dry; it returns the next tracks or an empty list at the end. */
        fun playQueue(context: Context, tracks: List<Track>, start: Int, loadMore: ((Track) -> List<Track>)?) {
            require(tracks.isNotEmpty()); tracks.forEach { require(allowed(context, it.url)) }
            pending = tracks; pendingMore = loadMore; pendingIndex = start.coerceIn(0, tracks.size - 1)
            ContextCompat.startForegroundService(context, Intent(context, NativePlaybackService::class.java).setAction(QUEUE))
        }
        fun cacheFile(context: Context, url: String): File {
            val digest = MessageDigest.getInstance("SHA-1").digest(url.toByteArray()).joinToString("") { "%02x".format(it) }
            return File(File(context.cacheDir, "segments").apply { mkdirs() }, "$digest.mp3")
        }
        /** Downloads one book segment into the shared cache. Returns true when the file is present afterwards. */
        fun download(context: Context, api: NativeApi, track: Track): Boolean {
            if (track.url.startsWith("file:") || track.bookId.isBlank()) return false
            val target = cacheFile(context, track.url)
            if (target.isFile) return true
            return runCatching {
                val audio = api.bytes(track.url.removePrefix(NavigationPolicy.ORIGIN), readTimeout = 300000)
                if (!audio.contentType.startsWith("audio/")) return false
                val part = File(target.path + ".part"); part.writeBytes(audio.bytes)
                if (!part.renameTo(target)) part.delete()
                target.isFile
            }.getOrDefault(false)
        }
        fun cachedCount(context: Context, tracks: List<Track>): Int = tracks.count { cacheFile(context, it.url).isFile }
        fun allowed(context: Context, url: String): Boolean {
            val uri = Uri.parse(url)
            if (NavigationPolicy.isAppOrigin(uri)) return true
            if (uri.scheme != "file") return false
            val path = File(uri.path ?: return false).canonicalPath
            return path.startsWith(context.filesDir.canonicalPath + File.separator) || path.startsWith(context.cacheDir.canonicalPath + File.separator)
        }
    }
    private lateinit var session: MediaSessionCompat
    private lateinit var manager: AudioManager
    private lateinit var focus: AudioFocusRequest
    private var player: MediaPlayer? = null
    private var ready = false
    private var seekOnPrepare = 0L
    private var sessionId = ""
    private var sessionBook = ""
    private var sessionMs = 0L
    private var sessionSegments = 0
    /** Listening sessions feed the statistics page; one session per book until playback stops. */
    private fun openSession(bookId: String) {
        if (sessionBook == bookId) { sessionSegments++; return }
        flushSession(); sessionBook = bookId; sessionId = ""; sessionMs = 0L; sessionSegments = 1
        io.execute { runCatching { (api.request("/api/audiobooks/listening-sessions", "POST", JSONObject().put("bookId", bookId)) as JSONObject).optString("id") }.getOrNull()?.let { id -> handler.post { if (sessionBook == bookId) sessionId = id } } }
    }
    private fun flushSession() {
        val id = sessionId; val ms = sessionMs; val segments = sessionSegments
        if (id.isBlank() || ms <= 0) return
        io.execute { runCatching { api.request("/api/audiobooks/listening-sessions", "PATCH", JSONObject().put("sessionId", id).put("durationMs", ms).put("segmentsPlayed", segments)) } }
    }
    private var rateApplied = false
    var rate = 1f; private set
    private val prefs by lazy { getSharedPreferences("native-workspaces", MODE_PRIVATE) }
    private var fadeApplied = false
    private fun positionKey(track: Track) = "pos:" + MessageDigest.getInstance("SHA-1").digest(track.url.toByteArray()).joinToString("") { "%02x".format(it) }
    /** Long non-book audio (Hörspiele, podcasts) remembers where it stopped. */
    private fun rememberPosition() {
        val t = track ?: return
        if (t.bookId.isNotBlank() || !ready || duration < 60000) return
        val pos = position
        if (pos < 5000 || pos > duration - 5000) prefs.edit().remove(positionKey(t)).apply() else prefs.edit().putLong(positionKey(t), pos).apply()
    }
    fun retry() { if (index >= 0) start(index) }
    private var sleepUntil = 0L
    private var sleepAtChapterEnd = false
    private var holdNext = false
    val sleepRemainingMs: Long get() = if (sleepUntil > 0) (sleepUntil - SystemClock.elapsedRealtime()).coerceAtLeast(0) else 0
    val sleepsAtChapterEnd: Boolean get() = sleepAtChapterEnd
    /** Position of the current segment inside its chapter, as (index, count). */
    val chapterPosition: Pair<Int, Int> get() { val t = track ?: return 0 to 0; if (t.bookId.isBlank()) return 0 to 0; val same = queue.filter { it.bookId == t.bookId && it.chapter == t.chapter }; return (same.indexOf(t) + 1) to same.size }
    fun sleep(minutes: Int) { sleepAtChapterEnd = false; sleepUntil = if (minutes > 0) SystemClock.elapsedRealtime() + minutes * 60000L else 0L; publish() }
    fun sleepUntilChapterEnd() { sleepUntil = 0L; sleepAtChapterEnd = true; publish() }
    var error: String? = null; private set
    private var resumeAfterFocus = false
    private val handler = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()
    private val api = NativeApi()
    private val queue = ArrayList<Track>()
    private var index = -1
    private var loadMore: ((Track) -> List<Track>)? = null
    private var loadingMore = false
    private var tickCount = 0
    private val ticker = object : Runnable { override fun run() {
        if (sleepUntil > 0) {
            val remaining = sleepUntil - SystemClock.elapsedRealtime()
            if (remaining <= 0) { sleepUntil = 0L; pause(); runCatching { player?.setVolume(1f, 1f) }; fadeApplied = false }
            else if (remaining < 20000 && playing) { val v = (remaining / 20000f).coerceIn(0.05f, 1f); runCatching { player?.setVolume(v, v) }; fadeApplied = true }
        }
        if (playing && track?.bookId?.isNotBlank() == true) { sessionMs += 1000; if (tickCount % 30 == 0) flushSession() }
        if (++tickCount % 10 == 0 && playing) rememberPosition()
        publish(); handler.postDelayed(this, 1000) } }
    private val noisy = object : BroadcastReceiver() { override fun onReceive(c: Context, i: Intent) { pause() } }
    val track: Track? get() = queue.getOrNull(index)
    val title: String get() = track?.title ?: "Vocarium"
    val subtitle: String get() = track?.subtitle.orEmpty()
    val hasNext: Boolean get() = index + 1 < queue.size || loadMore != null
    val hasPrevious: Boolean get() = index > 0
    val playing: Boolean get() = ready && player?.isPlaying == true
    val position: Long get() = if (ready) player?.currentPosition?.toLong() ?: 0 else 0
    val duration: Long get() = if (ready) player?.duration?.toLong()?.coerceAtLeast(0) ?: 0 else 0
    override fun onBind(intent: Intent?): IBinder? = null
    override fun onCreate() {
        super.onCreate(); current = this
        rate = prefs.getFloat("rate", 1f)
        getSystemService(NotificationManager::class.java).createNotificationChannel(NotificationChannel("native-audio", "Audiowiedergabe", NotificationManager.IMPORTANCE_LOW))
        manager = getSystemService(AudioManager::class.java)
        focus = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN).setAudioAttributes(attributes()).setOnAudioFocusChangeListener { change ->
            when (change) {
                AudioManager.AUDIOFOCUS_GAIN -> if (resumeAfterFocus) { resumeAfterFocus = false; resume() }
                AudioManager.AUDIOFOCUS_LOSS_TRANSIENT, AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK -> { val wasPlaying = playing; pause(); resumeAfterFocus = wasPlaying }
                AudioManager.AUDIOFOCUS_LOSS -> pause()
            }
        }.build()
        session = MediaSessionCompat(this, "Vocarium Native").apply {
            setCallback(object : MediaSessionCompat.Callback() {
                override fun onPlay() { resume() }; override fun onPause() { pause() }; override fun onStop() { stopSelf() }
                override fun onSeekTo(pos: Long) { seek(pos) }
                override fun onFastForward() { seek(position + 30000) }
                override fun onRewind() { seek(position - 15000) }
                override fun onSkipToNext() { next() }
                override fun onSkipToPrevious() { previous() }
            }); isActive = true
        }
        ContextCompat.registerReceiver(this, noisy, IntentFilter(AudioManager.ACTION_AUDIO_BECOMING_NOISY), ContextCompat.RECEIVER_NOT_EXPORTED)
        startForeground(410, notification()); handler.post(ticker)
    }
    private fun attributes() = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build()
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            QUEUE -> { val tracks = pending; pending = null; if (tracks != null) { seekOnPrepare = pendingSeekMs; pendingSeekMs = 0L; queue.clear(); queue.addAll(tracks); loadMore = pendingMore; pendingMore = null; loadingMore = false; start(pendingIndex) } }
            PLAY -> resume(); PAUSE -> pause(); STOP -> stopSelf(); NEXT -> next(); PREVIOUS -> previous()
        }
        return START_NOT_STICKY
    }
    private fun cacheFile(track: Track): File = cacheFile(this, track.url)
    /** Downloads a book segment ahead of time; the server renders on first request, so this hides generation latency. */
    private fun warm(track: Track) {
        if (track.url.startsWith("file:") || track.bookId.isBlank()) return
        io.execute { if (download(this, api, track)) trimCache() }
    }
    private val covers = android.util.LruCache<String, android.graphics.Bitmap>(4)
    private var coverLoading = ""
    /** Cover for the lock screen and the notification; fetched once per book. */
    private fun loadCover(bookId: String) {
        if (bookId.isBlank() || covers.get(bookId) != null || coverLoading == bookId) return
        coverLoading = bookId
        io.execute {
            val bitmap = runCatching {
                val audio = api.bytes("/api/audiobooks/${Uri.encode(bookId)}/cover", readTimeout = 20000)
                if (!audio.contentType.startsWith("image/")) return@runCatching null
                val bounds = android.graphics.BitmapFactory.Options().apply { inJustDecodeBounds = true }
                android.graphics.BitmapFactory.decodeByteArray(audio.bytes, 0, audio.bytes.size, bounds)
                val options = android.graphics.BitmapFactory.Options().apply { inSampleSize = maxOf(1, maxOf(bounds.outWidth, bounds.outHeight) / 512) }
                android.graphics.BitmapFactory.decodeByteArray(audio.bytes, 0, audio.bytes.size, options)
            }.getOrNull()
            handler.post { coverLoading = ""; if (bitmap != null) { covers.put(bookId, bitmap); publish() } }
        }
    }
    private fun trimCache() {
        val files = File(cacheDir, "segments").listFiles()?.filter { it.isFile }?.sortedBy { it.lastModified() } ?: return
        var total = files.sumOf { it.length() }
        for (file in files) { if (total <= 300L * 1024 * 1024) break; total -= file.length(); file.delete() }
    }
    private fun start(i: Int) {
        val track = queue.getOrNull(i) ?: return
        rememberPosition()
        index = i; ready = false; rateApplied = false; error = null
        player?.release()
        val next = MediaPlayer(); player = next
        next.setAudioAttributes(attributes())
        next.setWakeMode(this, PowerManager.PARTIAL_WAKE_LOCK)
        next.setOnPreparedListener { if (player === next) {
            ready = true
            if (seekOnPrepare > 0) { runCatching { next.seekTo(seekOnPrepare.toInt()) }; seekOnPrepare = 0L }
            else if (track.bookId.isBlank()) { val saved = prefs.getLong(positionKey(track), 0L); if (saved in 5000..(next.duration - 5000L)) { runCatching { next.seekTo(saved.toInt()) }; val m = saved / 60000; val sec = (saved / 1000) % 60; android.widget.Toast.makeText(this, "Fortgesetzt bei ${m}:${sec.toString().padStart(2, '0')}", android.widget.Toast.LENGTH_SHORT).show() } }
            if (holdNext) { holdNext = false; publish() } else resume() } }
        next.setOnCompletionListener { if (player === next) { track.takeIf { it.bookId.isBlank() }?.let { prefs.edit().remove(positionKey(it)).apply() }; finished() } }
        next.setOnErrorListener { _, _, _ -> if (player === next) { error = "Audio konnte nicht geladen werden. Bitte Verbindung und Anmeldung prüfen."; ready = false; manager.abandonAudioFocusRequest(focus); publish() }; true }
        try {
            val cached = cacheFile(track)
            when {
                track.bookId.isNotBlank() && cached.isFile -> next.setDataSource(cached.path)
                track.url.startsWith("file:") -> next.setDataSource(this, Uri.parse(track.url))
                else -> {
                    val headers = mutableMapOf("android-allow-cross-domain-redirect" to "0")
                    CookieManager.getInstance().getCookie(NavigationPolicy.ORIGIN)?.let { headers["Cookie"] = it }
                    next.setDataSource(this, Uri.parse(track.url), headers)
                }
            }
            next.prepareAsync(); publish()
        } catch (_: Exception) { error = "Audio konnte nicht geöffnet werden."; publish() }
        if (track.bookId.isNotBlank()) { openSession(track.bookId); loadCover(track.bookId); io.execute { runCatching { api.request("/api/audiobooks/${track.bookId}/progress", "POST", JSONObject().put("chapterIndex", track.chapter).put("segmentIndex", track.segment)) } } }
        for (ahead in 1..2) queue.getOrNull(i + ahead)?.let { warm(it) }
    }
    private fun finished() {
        val track = track
        val upcoming = queue.getOrNull(index + 1)
        if (sleepAtChapterEnd && track != null && (upcoming == null || upcoming.chapter != track.chapter || upcoming.bookId != track.bookId)) { sleepAtChapterEnd = false; holdNext = true }
        if (index + 1 < queue.size) { start(index + 1); return }
        val more = loadMore
        if (more != null && track != null && !loadingMore) {
            loadingMore = true
            io.execute {
                val extra = runCatching { more(track) }.getOrDefault(emptyList())
                handler.post { loadingMore = false; if (extra.isNotEmpty()) { queue.addAll(extra); start(index + 1) } else { loadMore = null; end(track) } }
            }
            return
        }
        end(track)
    }
    private fun end(track: Track?) {
        manager.abandonAudioFocusRequest(focus); publish()
        if (track != null && track.bookId.isNotBlank() && track.last) io.execute { runCatching { api.request("/api/audiobooks/${track.bookId}/progress", "POST", JSONObject().put("chapterIndex", track.chapter).put("segmentIndex", track.segment).put("completed", true)) } }
    }
    fun next() { if (index + 1 < queue.size) start(index + 1) else if (loadMore != null) finished() }
    fun previous() { if (ready && position > 4000) seek(0) else if (index > 0) start(index - 1) else seek(0) }
    fun resume() {
        if (!ready || manager.requestAudioFocus(focus) != AudioManager.AUDIOFOCUS_REQUEST_GRANTED) return
        if (fadeApplied) { runCatching { player?.setVolume(1f, 1f) }; fadeApplied = false }
        player?.start()
        if (!rateApplied) { rateApplied = true; if (rate != 1f) runCatching { player?.playbackParams = PlaybackParams().setSpeed(rate) } }
        publish()
    }
    fun pause() { resumeAfterFocus = false; if (ready) player?.pause(); rememberPosition(); flushSession(); manager.abandonAudioFocusRequest(focus); publish() }
    fun seek(ms: Long) { if (ready) player?.seekTo(ms.coerceIn(0, duration), MediaPlayer.SEEK_CLOSEST); publish() }
    fun speed(value: Float) { rate = value; prefs.edit().putFloat("rate", value).apply(); if (ready) { val wasPlaying = playing; runCatching { player?.playbackParams = PlaybackParams().setSpeed(value) }; rateApplied = true; if (!wasPlaying) player?.pause(); publish() } }
    private fun command(action: String) = PendingIntent.getService(this, action.hashCode(), Intent(this, NativePlaybackService::class.java).setAction(action), PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    private fun notification(): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, NativeActivity::class.java), PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val art = track?.bookId?.takeIf { it.isNotBlank() }?.let { covers.get(it) }
        val builder = NotificationCompat.Builder(this, "native-audio").setSmallIcon(R.drawable.ic_notification).setLargeIcon(art).setContentTitle(title).setContentText(error ?: subtitle.ifBlank { if (playing) "Wiedergabe" else "Pausiert" }).setContentIntent(open).setOnlyAlertOnce(true).setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
        val compact = ArrayList<Int>()
        if (hasPrevious) { builder.addAction(R.drawable.ic_prev, "Zurück", command(PREVIOUS)); compact.add(0) }
        builder.addAction(if (playing) R.drawable.ic_pause else R.drawable.ic_play, if (playing) "Pause" else "Abspielen", command(if (playing) PAUSE else PLAY)); compact.add(compact.size)
        if (hasNext) { builder.addAction(R.drawable.ic_next, "Weiter", command(NEXT)); compact.add(compact.size) }
        builder.addAction(android.R.drawable.ic_menu_close_clear_cancel, "Beenden", command(STOP))
        return builder.setStyle(androidx.media.app.NotificationCompat.MediaStyle().setMediaSession(session.sessionToken).setShowActionsInCompactView(*compact.toIntArray())).build()
    }
    private fun publish() {
        val art = track?.bookId?.takeIf { it.isNotBlank() }?.let { covers.get(it) }
        session.setMetadata(MediaMetadataCompat.Builder().putString(MediaMetadataCompat.METADATA_KEY_TITLE, title).putString(MediaMetadataCompat.METADATA_KEY_ARTIST, subtitle).putLong(MediaMetadataCompat.METADATA_KEY_DURATION, duration).apply { if (art != null) putBitmap(MediaMetadataCompat.METADATA_KEY_ALBUM_ART, art) }.build())
        val state = if (error != null) PlaybackStateCompat.STATE_ERROR else if (!ready) PlaybackStateCompat.STATE_BUFFERING else if (playing) PlaybackStateCompat.STATE_PLAYING else PlaybackStateCompat.STATE_PAUSED
        var actions = PlaybackStateCompat.ACTION_PLAY or PlaybackStateCompat.ACTION_PAUSE or PlaybackStateCompat.ACTION_STOP or PlaybackStateCompat.ACTION_SEEK_TO or PlaybackStateCompat.ACTION_FAST_FORWARD or PlaybackStateCompat.ACTION_REWIND
        if (hasNext) actions = actions or PlaybackStateCompat.ACTION_SKIP_TO_NEXT
        if (hasPrevious) actions = actions or PlaybackStateCompat.ACTION_SKIP_TO_PREVIOUS
        session.setPlaybackState(PlaybackStateCompat.Builder().setActions(actions).setState(state, position, if (playing) rate else 0f).build())
        getSystemService(NotificationManager::class.java).notify(410, notification())
    }
    override fun onDestroy() { rememberPosition(); flushSession(); handler.removeCallbacksAndMessages(null); unregisterReceiver(noisy); manager.abandonAudioFocusRequest(focus); player?.release(); player = null; session.release(); io.shutdownNow(); current = null; super.onDestroy() }
}
