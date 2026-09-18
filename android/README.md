# Vocarium für Android

Nativer Kotlin-Client (Paket `ch.zwaetschge.vocarium`) für die vier Bereiche Hörbücher,
Podcasts, Hörspiele und Sprachstudio. Er spricht direkt mit dem Gateway über HTTPS,
meldet sich über den Authelia-Login an (WebView nur für die Anmeldung) und spielt Audio
über einen Vordergrunddienst mit MediaSession (Sperrbildschirm, Bluetooth, Benachrichtigung).

- `app/src/main/java/ch/zwaetschge/vocarium/NativeActivity.kt`: alle Ansichten.
- `NativePlaybackService.kt`: Wiedergabe, Warteschlange, Schlummer-Timer, Segment-Cache.
- `NativeApi.kt`: authentifizierter Transport (JSON, Binär, Multipart, SSE).
- `NavigationPolicy.kt`: Ursprung (`ORIGIN`) und erlaubte Pfade; beim Selbsthosten anpassen.

Gebaut und installiert wird über den Android-Builder (siehe `docs/implementation/android-native-client/`
im internen Arbeitsverzeichnis). Dieser Ordner ist ein Spiegel des Builder-Workspaces;
`scripts/sync-android.sh` aktualisiert ihn.
