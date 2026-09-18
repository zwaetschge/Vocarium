package ch.zwaetschge.vocarium

import android.webkit.CookieManager
import org.json.JSONObject
import org.json.JSONTokener
import java.net.HttpURLConnection
import java.net.URL

class LoginRequired : Exception("Bitte bei Vocarium anmelden.")
class ApiBytes(val bytes: ByteArray, val contentType: String)

/** Native authenticated transport. Credentials never cross a redirect or enter logs. */
class NativeApi {
    private fun open(path: String, method: String, readTimeout: Int): HttpURLConnection {
        require(path.startsWith("/api/") && !path.contains("\\"))
        val connection = URL(NavigationPolicy.ORIGIN + path).openConnection() as HttpURLConnection
        connection.instanceFollowRedirects = false
        connection.connectTimeout = 15000
        connection.readTimeout = readTimeout
        connection.requestMethod = method
        connection.setRequestProperty("Accept", "application/json, */*")
        CookieManager.getInstance().getCookie(NavigationPolicy.ORIGIN)?.let { connection.setRequestProperty("Cookie", it) }
        return connection
    }
    private fun send(connection: HttpURLConnection, body: JSONObject?) {
        if (body == null) return
        connection.doOutput = true
        connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
        connection.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
    }
    private fun failure(connection: HttpURLConnection, status: Int): Exception {
        if (status == 401 || status in 300..399) return LoginRequired()
        val text = connection.errorStream?.bufferedReader()?.use { it.readText() }.orEmpty()
        val detail = runCatching { JSONObject(text).optString("detail") }.getOrDefault("")
        return Exception(detail.takeIf { it.isNotBlank() } ?: "Anfrage fehlgeschlagen (HTTP $status).")
    }
    private fun parse(connection: HttpURLConnection): Any {
        val text = connection.inputStream.bufferedReader().use { it.readText() }
        if (text.isBlank()) return JSONObject()
        if (connection.contentType?.contains("json") != true) throw LoginRequired()
        return JSONTokener(text).nextValue()
    }
    fun request(path: String, method: String = "GET", body: JSONObject? = null): Any {
        val connection = open(path, method, 60000)
        try {
            send(connection, body)
            val status = connection.responseCode
            if (status !in 200..299) throw failure(connection, status)
            return parse(connection)
        } finally { connection.disconnect() }
    }
    /** Binary answers such as generated speech or reference audio. */
    fun bytes(path: String, method: String = "GET", body: JSONObject? = null, readTimeout: Int = 600000): ApiBytes {
        val connection = open(path, method, readTimeout)
        try {
            send(connection, body)
            val status = connection.responseCode
            if (status !in 200..299) throw failure(connection, status)
            val type = connection.contentType.orEmpty()
            if (type.contains("text/html")) throw LoginRequired()
            return ApiBytes(connection.inputStream.use { it.readBytes() }, type)
        } finally { connection.disconnect() }
    }
    /** Server-sent events for long productions. Stops after the terminal `complete` or `error` event. */
    fun stream(path: String, method: String = "POST", body: JSONObject? = null, onEvent: (String, String) -> Unit) {
        val connection = open(path, method, 7200000)
        try {
            connection.setRequestProperty("Accept", "text/event-stream")
            if (body != null) send(connection, body) else if (method == "POST") { connection.doOutput = true; connection.outputStream.close() }
            val status = connection.responseCode
            if (status !in 200..299) throw failure(connection, status)
            if (connection.contentType?.contains("text/html") == true) throw LoginRequired()
            val reader = connection.inputStream.bufferedReader()
            var kind = "message"; val data = StringBuilder()
            while (true) {
                val line = reader.readLine() ?: break
                when {
                    line.isEmpty() -> if (data.isNotEmpty()) { val payload = data.toString(); data.setLength(0); val k = kind; kind = "message"; onEvent(k, payload); if (k == "complete" || k == "error") return }
                    line.startsWith(":") -> {}
                    line.startsWith("event:") -> kind = line.substring(6).trim()
                    line.startsWith("data:") -> { if (data.isNotEmpty()) data.append('\n'); data.append(line.substring(5).trimStart()) }
                }
            }
            throw Exception("Die Verbindung wurde vor dem Abschluss beendet.")
        } finally { connection.disconnect() }
    }
    /** Multipart upload for transcription and voice cloning. Long server work keeps the socket open. */
    fun upload(path: String, fields: Map<String, String>, fileField: String, fileName: String, mime: String, data: ByteArray, readTimeout: Int = 1800000): Any {
        val boundary = "----Vocarium" + System.nanoTime()
        val connection = open(path, "POST", readTimeout)
        try {
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
            connection.setChunkedStreamingMode(64 * 1024)
            connection.outputStream.buffered().use { out ->
                fun line(text: String) = out.write((text + "\r\n").toByteArray(Charsets.UTF_8))
                for ((key, value) in fields) { line("--$boundary"); line("Content-Disposition: form-data; name=\"$key\""); line(""); line(value) }
                line("--$boundary"); line("Content-Disposition: form-data; name=\"$fileField\"; filename=\"${fileName.replace("\"", "_")}\""); line("Content-Type: $mime"); line("")
                out.write(data); line(""); line("--$boundary--")
            }
            val status = connection.responseCode
            if (status !in 200..299) throw failure(connection, status)
            return parse(connection)
        } finally { connection.disconnect() }
    }
}
