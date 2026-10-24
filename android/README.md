# APA for Android (`id.frostfire.apa`)

The phone app: bank notification listener, morning briefing playback, health, settings.
Build, install, connect and troubleshoot: [../docs/ANDROID.md](../docs/ANDROID.md).

Quick build (JDK 21, Android SDK, Gradle 9.3):

```bash
echo "sdk.dir=/path/to/Android/Sdk" > local.properties
gradle :app:assembleDebug :app:testDebugUnitTest
```

The APK is `app/build/outputs/apk/debug/app-debug.apk`.
