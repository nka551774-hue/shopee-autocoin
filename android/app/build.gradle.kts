plugins {
    id("com.android.application")
}

android {
    namespace = "tw.avianjay.autocoin"

    compileSdk {
        version = release(36)
    }

    // CI 用 secrets 簽 release；本機沒設環境變數就只做 debug，不受影響
    val ksPath = System.getenv("KEYSTORE_PATH")
    val ksAlias = System.getenv("KEYSTORE_ALIAS")
    val ksPass = System.getenv("KEYSTORE_PASSWORD")
    val hasKeystore = !ksPath.isNullOrBlank() && !ksAlias.isNullOrBlank() && !ksPass.isNullOrBlank()

    defaultConfig {
        applicationId = "tw.avianjay.autocoin"
        minSdk { version = release(30) }
        targetSdk { version = release(36) }
        versionCode = (System.getenv("VERSION_CODE") ?: "1").toInt()
        versionName = System.getenv("VERSION_NAME") ?: "0.1.0"
    }

    signingConfigs {
        if (hasKeystore) {
            create("release") {
                storeFile = file(ksPath!!)
                storePassword = ksPass
                keyAlias = ksAlias
                keyPassword = ksPass
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (hasKeystore) signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    testOptions {
        // Local tests exercise state, parsers and lifecycle guards without a phone.
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20260814")
}
