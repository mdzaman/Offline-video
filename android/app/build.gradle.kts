plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// The app version comes from the Python package, so the APK and the desktop apps always match.
val appVersion: String = Regex("__version__ = \"([^\"]+)\"")
    .find(rootDir.resolve("../offline_video/__init__.py").readText())!!.groupValues[1]
val (major, minor, patch) = appVersion.split(".").map { it.toInt() }

// Release signing: set ANDROID_KEYSTORE_FILE, ANDROID_KEYSTORE_PASSWORD, ANDROID_KEY_ALIAS and
// ANDROID_KEY_PASSWORD (GitHub secrets in CI). Without them the debug key is used.
val keystoreFile = System.getenv("ANDROID_KEYSTORE_FILE")?.let { file(it) }?.takeIf { it.exists() }

android {
    namespace = "io.github.mdzaman.offlinevideo"
    compileSdk = 35

    defaultConfig {
        applicationId = "io.github.mdzaman.offlinevideo"
        minSdk = 29
        targetSdk = 35
        versionName = appVersion
        versionCode = major * 10000 + minor * 100 + patch
    }

    signingConfigs {
        if (keystoreFile != null) {
            create("release") {
                storeFile = keystoreFile
                storePassword = System.getenv("ANDROID_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("ANDROID_KEY_ALIAS")
                keyPassword = System.getenv("ANDROID_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (keystoreFile != null) signingConfigs.getByName("release") else signingConfigs.getByName("debug")
        }
    }

    // One APK per processor type (smaller downloads) plus a universal one.
    splits {
        abi {
            isEnable = true
            reset()
            include("arm64-v8a", "armeabi-v7a", "x86_64")
            isUniversalApk = true
        }
    }

    // Python, FFmpeg and QuickJS are programs shipped as .so files; Android may only run them from the
    // extracted native-library folder, so the libraries must be extracted at install time.
    packaging {
        jniLibs { useLegacyPackaging = true }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { buildConfig = true }

    sourceSets["main"].assets.srcDir(layout.buildDirectory.dir("generated/pyapp"))
}

// Bundle the Offline Video Python app and its pure-Python dependencies into assets/pyapp.zip.
val bundlePython by tasks.registering(Exec::class) {
    val script = rootDir.resolve("build_python_bundle.py")
    val out = layout.buildDirectory.dir("generated/pyapp").get().asFile
    inputs.dir(rootDir.resolve("../offline_video"))
    inputs.file(script)
    outputs.dir(out)
    commandLine(System.getenv("PYTHON") ?: "python3", script.path, out.path)
}
tasks.named("preBuild") { dependsOn(bundlePython) }

dependencies {
    // Python 3.12, yt-dlp support files, FFmpeg/FFprobe and QuickJS built for Android.
    implementation("io.github.junkfood02.youtubedl-android:library:0.18.1")
    implementation("io.github.junkfood02.youtubedl-android:ffmpeg:0.18.1")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.activity:activity-ktx:1.9.3")
}
