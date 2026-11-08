package io.github.mdzaman.offlinevideo

import android.Manifest
import android.annotation.SuppressLint
import android.content.ContentValues
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.MimeTypeMap
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.addCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import java.io.File
import java.net.URLEncoder
import kotlin.concurrent.thread

/** Shows the Offline Video web app, served by the Python engine on this phone. */
class MainActivity : ComponentActivity() {
    private lateinit var root: FrameLayout
    private lateinit var web: WebView
    private lateinit var status: TextView
    private var loaded = false
    private var pendingShare: String? = null
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private var fullscreenView: View? = null

    private val pickFile = registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        fileCallback?.onReceiveValue(uri?.let { arrayOf(it) })
        fileCallback = null
    }
    private val askNotifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) {}

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        root = FrameLayout(this)
        web = WebView(this)
        status = TextView(this).apply {
            text = getString(R.string.starting)
            gravity = Gravity.CENTER
            textSize = 16f
            setPadding(48, 48, 48, 48)
        }
        root.addView(web, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        root.addView(status, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER))
        setContentView(root)
        // Keep the page clear of the status bar, navigation bar, camera cut-out and keyboard.
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout() or WindowInsetsCompat.Type.ime()
            )
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            WindowInsetsCompat.CONSUMED
        }

        setUpWebView()
        if (Build.VERSION.SDK_INT >= 33) askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        pendingShare = sharedText(intent)

        onBackPressedDispatcher.addCallback(this) {
            if (fullscreenView != null) {
                (web.webChromeClient as? WebChromeClient)?.onHideCustomView()
                return@addCallback
            }
            // Let the page close its video player first.
            web.evaluateJavascript("window.ovBack ? ovBack() : false") { handled ->
                if (handled != "true") {
                    if (web.canGoBack()) web.goBack() else moveTaskToBack(true)
                }
            }
        }
    }

    override fun onResume() {
        super.onResume()
        ContextCompat.startForegroundService(this, Intent(this, ServerService::class.java))
        if (!loaded) {
            waitForEngine()
        } else {
            // Reopened after the engine was stopped from the notification: start it again.
            thread {
                if (!Backend.isReady()) runOnUiThread {
                    loaded = false
                    waitForEngine()
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        sharedText(intent)?.let { text ->
            if (loaded) web.loadUrl(Backend.url + "/?add=" + URLEncoder.encode(text, "UTF-8")) else pendingShare = text
        }
    }

    /** Text shared from another app (e.g. YouTube's Share → Offline Video). */
    private fun sharedText(intent: Intent?): String? =
        if (intent?.action == Intent.ACTION_SEND) intent.getStringExtra(Intent.EXTRA_TEXT) else null

    private fun waitForEngine() {
        status.visibility = View.VISIBLE
        status.text = getString(R.string.starting)
        thread(name = "wait-engine") {
            val deadline = System.currentTimeMillis() + 180_000
            while (System.currentTimeMillis() < deadline) {
                if (Backend.isReady()) {
                    runOnUiThread { showApp() }
                    return@thread
                }
                Backend.error?.let { err ->
                    runOnUiThread { status.text = err }
                    return@thread
                }
                Thread.sleep(300)
            }
            runOnUiThread { status.text = getString(R.string.start_timeout) }
        }
    }

    private fun showApp() {
        if (loaded) return
        loaded = true
        status.visibility = View.GONE
        val share = pendingShare?.let { "/?add=" + URLEncoder.encode(it, "UTF-8") } ?: "/"
        pendingShare = null
        web.loadUrl(Backend.url + share)
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setUpWebView() {
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            allowFileAccess = false
        }
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val uri = request.url
                if (uri.host == "127.0.0.1") return false
                // Links to other sites (e.g. a video's YouTube page) open in the browser.
                runCatching { startActivity(Intent(Intent.ACTION_VIEW, uri)) }
                return true
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams,
            ): Boolean {
                fileCallback?.onReceiveValue(null)
                fileCallback = callback
                pickFile.launch("text/*")
                return true
            }

            // Full-screen video
            override fun onShowCustomView(view: View, callback: CustomViewCallback) {
                fullscreenView = view
                root.addView(view, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
                WindowInsetsControllerCompat(window, root).apply {
                    hide(WindowInsetsCompat.Type.systemBars())
                    systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
                }
            }

            override fun onHideCustomView() {
                fullscreenView?.let { root.removeView(it) }
                fullscreenView = null
                WindowInsetsControllerCompat(window, root).show(WindowInsetsCompat.Type.systemBars())
            }
        }
        // "Save to phone": copy the file from the app's library into the phone's Movies / Music folder.
        web.setDownloadListener { url, _, _, _, _ -> saveToPhone(Uri.parse(url)) }
    }

    private fun saveToPhone(uri: Uri) {
        val rel = uri.path?.removePrefix("/media/") ?: return
        val library = Backend.libraryDir(this)
        val src = File(library, rel)
        if (!src.canonicalPath.startsWith(library.canonicalPath + File.separator) || !src.isFile) return
        val ext = src.extension.lowercase()
        val mime = MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext) ?: "application/octet-stream"
        val (collection, folder) = when {
            mime.startsWith("video/") -> MediaStore.Video.Media.EXTERNAL_CONTENT_URI to Environment.DIRECTORY_MOVIES
            mime.startsWith("audio/") -> MediaStore.Audio.Media.EXTERNAL_CONTENT_URI to Environment.DIRECTORY_MUSIC
            else -> MediaStore.Downloads.EXTERNAL_CONTENT_URI to Environment.DIRECTORY_DOWNLOADS
        }
        Toast.makeText(this, getString(R.string.saving, src.name), Toast.LENGTH_SHORT).show()
        thread(name = "save-to-phone") {
            val result = runCatching {
                val values = ContentValues().apply {
                    put(MediaStore.MediaColumns.DISPLAY_NAME, src.name)
                    put(MediaStore.MediaColumns.MIME_TYPE, mime)
                    put(MediaStore.MediaColumns.RELATIVE_PATH, "$folder/Offline Video")
                    put(MediaStore.MediaColumns.IS_PENDING, 1)
                }
                val item = contentResolver.insert(collection, values) ?: error("no space")
                contentResolver.openOutputStream(item)!!.use { out -> src.inputStream().use { it.copyTo(out) } }
                values.clear()
                values.put(MediaStore.MediaColumns.IS_PENDING, 0)
                contentResolver.update(item, values, null, null)
            }
            runOnUiThread {
                val msg = if (result.isSuccess) getString(R.string.saved, "$folder/Offline Video")
                else getString(R.string.save_failed, result.exceptionOrNull()?.message ?: "")
                Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
            }
        }
    }
}
