package ch.zwaetschge.vocarium

import android.net.Uri

/** App links and remembered locations never include login or API endpoints. */
object NavigationPolicy {
    const val ORIGIN = "https://vocarium.zwaetschge-webui.ch"
    const val HOME = "$ORIGIN/audiobooks"
    val modes = listOf("Hörbücher" to "/audiobooks", "Podcasts" to "/podcast", "Hörspiele" to "/hoerspiele", "Sprachstudio" to "/")
    fun isAppOrigin(uri: Uri): Boolean = uri.scheme == "https" && uri.host == "vocarium.zwaetschge-webui.ch" && (uri.port == -1 || uri.port == 443) && uri.userInfo == null
    fun isWorkspace(uri: Uri): Boolean {
        if (!isAppOrigin(uri)) return false
        val path = uri.path ?: "/"
        return path == "/" || listOf("audiobooks", "podcast", "hoerspiele", "library", "settings", "voices", "clone", "transcribe").any { path == "/$it" || path.startsWith("/$it/") }
    }
    fun isInternal(uri: Uri): Boolean = uri.scheme == "https" && uri.host?.endsWith(".zwaetschge-webui.ch") == true && uri.userInfo == null
    fun route(uri: Uri): String = (uri.encodedPath?.takeIf { it.isNotEmpty() } ?: "/") + (uri.encodedQuery?.let { "?$it" } ?: "") + (uri.encodedFragment?.let { "#$it" } ?: "")
}
