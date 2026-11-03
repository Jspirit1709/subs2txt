[app]
title = Subs2Txt
package.name = subs2txt
package.domain = org.fono
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
version = 1.0
requirements = python3,kivy,yt-dlp,certifi,openssl,sqlite3,urllib3,websockets,brotli,mutagen,pycryptodomex
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE
android.api = 33
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
