# Android app (`id.frostfire.apa`)

The phone side of the system. Its parts are independent and optional:

| Feature | How it works |
|---|---|
| **Bank and e-wallet notifications** | A notification listener (one-time "notification access") keeps only notifications of the banking and wallet apps you tick (Indonesian banks, GoPay, OVO, DANA, ShopeePay... built in) that mention an amount. It drops one-time codes, promotions and bill reminders, **scrubs card, account, phone, ID and e-mail numbers on the phone**, buffers them locally and syncs them to the server with an idempotency key. |
| **Morning briefing** | When the alarm notification of the clock app disappears (the alarm was dismissed), the app fetches today's briefing and plays it: the server's natural voice (Gemini or Microsoft), or the phone's own voice when the server cannot make audio. The detailed text version also goes to Telegram. Fallback: a scheduled time. |
| **Briefing on Bluetooth** | Optional: plays the briefing when a car or headset connects. |
| **Health** | Reads sleep, steps, calories and resting heart rate from Health Connect (the Huawei Health app writes there) and posts them. Read-only. |
| **Settings** | Server address and key, language, voice (with a sample), news topics, words to follow, weather by GPS or city, the background checklist. The choices are stored on the server and are the same as in the web dashboard. |

## Build the APK

Requirements: JDK 21 and the Android SDK (platform 36). Open the `android/` folder in Android Studio and run, or from a
terminal with Gradle 9.3 installed (there is no Gradle wrapper in the repo):

```bash
cd android
echo "sdk.dir=/path/to/Android/Sdk" > local.properties     # Windows: sdk.dir=C\:/Users/you/AppData/Local/Android/Sdk
gradle :app:assembleDebug :app:testDebugUnitTest
```

The APK is `android/app/build/outputs/apk/debug/app-debug.apk`. It is a debug build (signed with the debug key), fine for
your own phone. GitHub Actions can build it for you: see `.github/workflows/ci.yml` (the APK is attached to each run as an
artifact).

## Install

1. Phone: enable Developer options and USB debugging, connect it, then `adb install -r app-debug.apk`. Xiaomi/Redmi phones also
   need "Install via USB" in Developer options. Or copy the APK to the phone and open it (allow installs from that source).
2. Open **APA**. Under *Server*, enter your server address (for example `http://192.168.1.10:8000`, or your VPN address) and
   the `INGEST_API_KEY`, then press **Save server & test connection**. It should say "Connected".
3. Grant the permissions the checklist asks for: notification access (for bank notifications), app notifications (for the
   briefing), and Health Connect. Pick the bank apps to listen to.
4. Open **Morning briefing** settings: language, voice, topics, words to follow, weather (GPS or a city). Press the sample button.

## Keep it working in the background

Some phones (Xiaomi, Oppo, Vivo, Huawei, Samsung) freeze apps that are closed. The settings screen has a checklist with a tick or a cross
for each requirement and buttons for: switching battery optimisation off for APA, opening your maker's autostart screen,
re-binding the notification listener, the resend interval (15 to 120 minutes) and "Wi-Fi only". Nothing is lost while the phone
is offline or frozen: the buffer waits and is resent with the same idempotency keys.

## Security

- The key is stored encrypted with a hardware-backed key. Optional request signing: set `INGEST_HMAC_SECRET` on the server and enter the same secret in the app.
- Plain HTTP to a private address is allowed; every request carries the key, so use your home network or a VPN, never the open internet.
- GPS: only a position rounded to about 1 km, only when GPS weather is on, only for the weather, and the server forgets it when you switch it off.

## What is tested

- Builds and unit-tests under Gradle (AGP 8.13, Kotlin 2.2, compileSdk 36): the notification filter, scrubber, idempotency
  key, alarm-dismissal rule, speech chunking, wake-time calculation, health payload, background helper and location rule.
- Installed on a real phone (settings screen, server connection, language and voice lists), and installed and started on an x86_64
  emulator. The notification listener and the alarm detection depend on your phone and clock app: if your clock app hides
  its notification, use the scheduled fallback time.
- Not tested: Health Connect with real Huawei data, and every phone maker's autostart screen.

## Language of the app

The app's own screens are in English. Notification keyword lists are Indonesian on purpose (they match Indonesian bank
messages). The briefing language is a setting.
