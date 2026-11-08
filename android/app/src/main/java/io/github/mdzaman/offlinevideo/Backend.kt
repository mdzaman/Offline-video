package io.github.mdzaman.offlinevideo

import android.content.Context
import android.os.Environment
import android.system.Os
import android.util.Log
import com.yausername.ffmpeg.FFmpeg
import com.yausername.youtubedl_android.YoutubeDL
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.ServerSocket
import java.net.URL
import java.util.zip.ZipInputStream
import kotlin.concurrent.thread

/**
 * Runs the Offline Video Python server on the phone.
 *
 * Python 3.12, FFmpeg/FFprobe and QuickJS come from youtubedl-android: they are shipped as .so files in the
 * app's native-library folder (the only place Android lets an app run programs from), with their support
 * files extracted by YoutubeDL.init / FFmpeg.init. The Offline Video code itself is assets/pyapp.zip.
 */
object Backend {
    private const val TAG = "OfflineVideo"
    private const val PREFERRED_PORT = 48623

    @Volatile var port = 0
        private set
    @Volatile var error: String? = null
        private set
    private var process: Process? = null
    private val log = ArrayDeque<String>()

    val url get() = "http://127.0.0.1:$port"

    fun libraryDir(context: Context): File =
        File(context.getExternalFilesDir(Environment.DIRECTORY_MOVIES) ?: context.filesDir, "Offline Video")

    /** Starts the server if it isn't running. Blocking: call from a background thread. */
    @Synchronized
    fun start(context: Context) {
        if (process?.isAlive == true) return
        error = null
        try {
            YoutubeDL.getInstance().init(context)
            FFmpeg.getInstance().init(context)
            val app = extractApp(context)
            val native = File(context.applicationInfo.nativeLibraryDir)
            val bin = linkTools(context, native)
            val packages = File(context.noBackupFilesDir, "youtubedl-android/packages")
            val python = File(packages, "python")
            val ffmpeg = File(packages, "ffmpeg")

            port = choosePort()
            if (isOurs(port, sameVersion = true)) return // still running from before (e.g. the app was reopened)
            if (isOurs(port, sameVersion = false)) {
                post("/api/shutdown") // an older version after an update: replace it
                Thread.sleep(1000)
            }

            val library = libraryDir(context).apply { mkdirs() }
            val pb = ProcessBuilder(
                File(native, "libpython.so").path, "-u", "-m", "offline_video",
                "--no-browser", "--port", port.toString(), "--dir", library.path,
            ).directory(context.filesDir).redirectErrorStream(true)
            pb.environment().apply {
                put("LD_LIBRARY_PATH", "$python/usr/lib:$ffmpeg/usr/lib")
                put("SSL_CERT_FILE", "$python/usr/etc/tls/cert.pem")
                put("PYTHONHOME", "$python/usr")
                put("PYTHONPATH", app.path)
                put("HOME", context.filesDir.path) // settings are kept in ~/.offline_video.json
                put("TMPDIR", context.cacheDir.path)
                put("PATH", "$bin:$native:${System.getenv("PATH")}")
                put("OFFLINE_VIDEO_ANDROID", "1")
                put("OFFLINE_VIDEO_QJS", File(native, "libqjs.so").path)
            }
            val proc = pb.start()
            process = proc
            thread(name = "python-log", isDaemon = true) {
                proc.inputStream.bufferedReader().forEachLine { line ->
                    Log.i(TAG, line)
                    synchronized(log) {
                        log.addLast(line)
                        if (log.size > 40) log.removeFirst()
                    }
                }
                val code = proc.waitFor()
                if (process === proc) error = "The download engine stopped (code $code).\n" + recentLog()
            }
        } catch (e: Exception) {
            Log.e(TAG, "start failed", e)
            error = "Couldn't start the download engine: ${e.message}"
        }
    }

    @Synchronized
    fun stop() {
        process?.let { p ->
            process = null
            runCatching { post("/api/shutdown") }
            p.destroy()
        }
    }

    /** True once the web server answers. */
    fun isReady(): Boolean = port != 0 && runCatching { get("/api/status") }.isSuccess

    fun hasActiveDownloads(): Boolean = runCatching {
        val jobs = JSONObject(get("/api/jobs")).getJSONArray("jobs")
        (0 until jobs.length()).any { jobs.getJSONObject(it).getString("status") in setOf("queued", "downloading", "processing") }
    }.getOrDefault(false)

    private fun recentLog() = synchronized(log) { log.takeLast(12).joinToString("\n") }

    /** Unpacks assets/pyapp.zip into files/pyapp, only when the bundle changed (first run or update). */
    private fun extractApp(context: Context): File {
        val dir = File(context.filesDir, "pyapp")
        val stamp = File(dir, ".sha256")
        val wanted = context.assets.open("pyapp.sha256").bufferedReader().use { it.readText().trim() }
        if (stamp.exists() && stamp.readText() == wanted) return dir
        dir.deleteRecursively()
        dir.mkdirs()
        ZipInputStream(context.assets.open("pyapp.zip")).use { zip ->
            while (true) {
                val entry = zip.nextEntry ?: break
                val out = File(dir, entry.name)
                require(out.canonicalPath.startsWith(dir.canonicalPath)) { "bad zip entry" }
                if (entry.isDirectory) out.mkdirs() else {
                    out.parentFile?.mkdirs()
                    out.outputStream().use { zip.copyTo(it) }
                }
            }
        }
        stamp.writeText(wanted)
        return dir
    }

    /** files/bin/ffmpeg, ffprobe and qjs, pointing at the bundled programs, so they can be found by name. */
    private fun linkTools(context: Context, native: File): File {
        val bin = File(context.filesDir, "bin").apply { mkdirs() }
        for ((name, lib) in listOf("ffmpeg" to "libffmpeg.so", "ffprobe" to "libffprobe.so", "qjs" to "libqjs.so")) {
            val target = File(native, lib)
            val link = File(bin, name)
            link.delete()
            if (target.exists()) Os.symlink(target.path, link.path)
        }
        return bin
    }

    private fun choosePort(): Int {
        if (portFree(PREFERRED_PORT) || isOurs(PREFERRED_PORT, sameVersion = false)) return PREFERRED_PORT
        return ServerSocket(0, 1, InetAddress.getByName("127.0.0.1")).use { it.localPort }
    }

    private fun portFree(p: Int) = runCatching {
        ServerSocket(p, 1, InetAddress.getByName("127.0.0.1")).close()
    }.isSuccess

    private fun isOurs(p: Int, sameVersion: Boolean): Boolean = runCatching {
        val status = JSONObject(get("/api/status", p))
        status.optBoolean("android") && (!sameVersion || status.optString("version") == BuildConfig.VERSION_NAME)
    }.getOrDefault(false)

    private fun get(path: String, p: Int = port): String {
        val c = URL("http://127.0.0.1:$p$path").openConnection() as HttpURLConnection
        c.connectTimeout = 1500
        c.readTimeout = 5000
        return c.inputStream.bufferedReader().use { it.readText() }
    }

    private fun post(path: String) {
        val c = URL("$url$path").openConnection() as HttpURLConnection
        c.requestMethod = "POST"
        c.connectTimeout = 1500
        c.readTimeout = 5000
        c.responseCode
    }
}
