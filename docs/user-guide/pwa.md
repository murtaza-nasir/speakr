# Progressive Web App (PWA)

Speakr is a Progressive Web App that can be installed on your device for a more native-app like experience and wake lock support to prevent screen sleep during recording.

## What is a PWA?

A Progressive Web App combines the best of web and mobile apps:

- **Installable** - Add to your home screen like a native app
- **Fast loading** - Cached assets load instantly
- **Wake lock** - Prevents screen from auto-sleeping during recording
- **No app store** - Install directly from your browser
- **Auto-updates** - Always get the latest version automatically

## Installing Speakr as a PWA

### On Android (Chrome/Edge)

1. **Open Speakr** in Chrome or Edge browser
2. Look for the **"Add to Home Screen"** prompt at the bottom of the screen
3. Tap **"Add"** or **"Install"**
4. Alternatively:
   - Tap the three-dot menu (⋮) in the browser
   - Select **"Add to Home screen"** or **"Install app"**
   - Follow the prompts

5. **Launch** the app from your home screen

!!! tip "Banner Prompt"
    If you don't see the install prompt, you may need to visit Speakr a few times first. The browser will offer installation after detecting regular usage.

### On iOS (Safari)

1. **Open Speakr** in Safari
2. Tap the **Share** button (□↑) at the bottom of the screen
3. Scroll down and tap **"Add to Home Screen"**
4. Edit the name if desired and tap **"Add"**
5. **Launch** the app from your home screen

!!! note "iOS Limitations"
    iOS has some restrictions on PWAs:
    - Wake Lock API requires iOS 16.4+ (Safari 16.4+)
    - Background execution is more limited than Android
    - Some features work better on iOS 17+

### On Desktop (Chrome/Edge/Brave)

1. **Open Speakr** in your browser
2. Look for the **install icon** (⊕) in the address bar
3. Click it and select **"Install"**
4. Alternatively:
   - Click the three-dot menu
   - Select **"Install Speakr"** or **"Add to applications"**

5. **Launch** from your applications menu or desktop shortcut

## PWA Features

### Offline Support

Once installed, Speakr caches essential files for offline use:

- Application interface and UI
- CSS stylesheets and fonts
- JavaScript application code
- Icons and images

Note: API calls and file uploads require internet connection.

!!! info "Offline Capabilities"
    While you can access the app offline, transcription and AI features require an internet connection to your configured API endpoints.

### Mobile Recording Features

#### Wake Lock API
Prevents your device screen from auto-sleeping while app is visible:

- **Automatic activation** - Enabled when you start recording
- **Keeps screen on** - Prevents screen from turning off while app is in foreground
- **Auto-recovery** - Re-acquires if released during recording
- **Battery consideration** - Only active during recording sessions

#### Persistent Notifications
Shows recording status in your notification tray (mobile only):

- **Status indicator** - Visual reminder that recording is active
- **One-tap return** - Tap notification to return to the app
- **Silent** - No sound or vibration

#### Page Visibility Detection
Monitors when the app goes to background:

- **Detects minimization** - Knows when app is backgrounded
- **Smart recovery** - Re-activates wake lock when returning to app
- **State awareness** - Tracks recording state across visibility changes

### How Background Recording Works

!!! danger "Critical Limitation: Keep App Visible on Mobile"
    **Mobile browsers (Chrome, Safari, etc.) suspend audio recording when the app is minimized or the screen is locked.** This is a fundamental browser limitation that cannot be overcome with PWA features.

    **What this means:**
    - Recording **will pause** if you minimize the window
    - Recording **will pause** if you lock your screen
    - Recording **will pause** if you switch to another app
    - Recording **continues** if you keep Speakr visible in foreground
    - Wake lock **prevents screen from auto-sleeping** while Speakr is visible

    **For long meetings on mobile**, you have two options:
    1. **Keep Speakr visible** - Don't minimize, lock screen, or switch apps
    2. **Use native recorder** - Use your phone's built-in voice recorder app, then upload the file to Speakr afterward

**Why this happens:**

Mobile browsers intentionally suspend web pages in the background to save battery. This affects:
- JavaScript execution (timers, code)
- MediaRecorder API (audio capture)
- Audio context (audio processing)

Native apps don't have this limitation because they use platform-specific APIs that run outside the browser.

**Desktop browsers work differently:**
- Recording continues when window is minimized
- Can switch to other apps
- Only stops when browser is completely closed

**Starting a recording on mobile (with limitations):**

1. Start recording (microphone or system audio)
2. Wake lock prevents screen from auto-sleeping
3. Notification appears in notification tray
4. **Keep Speakr visible** in the foreground
5. Do not minimize, lock screen, or switch apps

**If you accidentally minimize:**

- Recording audio will pause (silence)
- Timer continues counting
- When you return to app, recording resumes
- Silent gap will be in the final recording

## Permissions

### Required Permissions

#### Microphone Access
- **When**: Starting a microphone recording
- **Why**: Capture audio from your device's microphone
- **Scope**: Only active during recording

#### System Audio Access (Desktop)
- **When**: Recording system audio or both mic + system
- **Why**: Capture audio from browser tabs, applications
- **Scope**: Only active during recording

### Recommended Permissions

#### Notification Permission (Mobile)
- **When**: First recording on mobile device
- **Why**: Show visual reminder that recording is active
- **Impact**: Provides quick access to return to app
- **Scope**: Only shown during active recordings

!!! tip "Granting Permissions"
    If you accidentally deny a permission, you can reset it in your browser/device settings:

    - **Android Chrome**: Settings → Site settings → Speakr URL → Permissions
    - **iOS Safari**: Settings → Safari → Speakr URL → Permissions
    - **Desktop**: Click the lock icon (🔒) in the address bar → Permissions

## Service Worker

Speakr uses a service worker to provide PWA capabilities:

### What it Does

- **Caches static assets** - For offline access and faster loading
- **Manages updates** - Automatically updates cached content
- **Handles notifications** - Manages persistent recording notifications
- **Background sync** - Foundation for future features like upload retry

### Viewing Service Worker Status

**Chrome DevTools:**

1. Press F12 to open DevTools
2. Go to **Application** tab
3. Select **Service Workers** in the sidebar
4. See registration status and version

**Console Logs:**

The service worker logs its activity to the browser console:

```
[Service Worker] Installing...
[Service Worker] Caching static assets
[Service Worker] Activating...
[Service Worker] Script loaded
```

### Updating Service Worker

The service worker automatically updates:

- **Check interval**: Every 60 seconds while app is open
- **Update process**: Downloads new version in background
- **Activation**: Takes effect on next page reload

To force an update:

1. Close all Speakr tabs/windows
2. Reopen Speakr
3. New service worker activates automatically

## Browser Compatibility

### Mobile Browsers

| Feature | Chrome Android | Safari iOS | Samsung Internet |
|---------|----------------|------------|------------------|
| PWA Install | ✅ Android 5+ | ✅ iOS 11.3+ | ✅ 4.0+ |
| Service Worker | ✅ Chrome 40+ | ✅ iOS 11.3+ | ✅ 4.0+ |
| Wake Lock | ✅ Chrome 84+ | ✅ iOS 16.4+ | ✅ 13.0+ |
| Notifications | ✅ Chrome 42+ | ✅ iOS 16.4+ | ✅ 4.0+ |
| Page Visibility | ✅ All versions | ✅ All versions | ✅ All versions |

### Desktop Browsers

| Feature | Chrome | Edge | Brave | Firefox | Safari |
|---------|--------|------|-------|---------|--------|
| PWA Install | ✅ 73+ | ✅ 79+ | ✅ All | ⚠️ Limited | ⚠️ Limited |
| Service Worker | ✅ 40+ | ✅ 17+ | ✅ All | ✅ 44+ | ✅ 11.1+ |
| Wake Lock | ✅ 84+ | ✅ 84+ | ✅ All | ❌ | ✅ 16.4+ |

!!! info "Firefox & Safari Desktop"
    Firefox and Safari have limited PWA install support on desktop but all core features work in the browser.

## Troubleshooting

### Recording Stops When Screen Locks

**This is expected behavior on mobile browsers.** Recording will pause when you minimize the app or lock the screen.

**Solutions:**

1. **Keep app visible** - Don't minimize or lock screen during recording
2. **Wake lock prevents auto-sleep** - Screen won't turn off automatically while recording
3. **Use native recorder** - For long meetings, use your phone's built-in voice recorder
4. **Desktop works differently** - Recording continues when minimized on desktop browsers

### PWA Not Offered for Installation

**Solutions:**

1. **Visit the app multiple times** - Browser may require several visits
2. **Check HTTPS** - PWA requires secure connection (https://)
3. **Try different browser** - Use Chrome/Edge for best support
4. **Clear browser cache** - Force refresh of manifest

### Service Worker Not Registering

**Check console for errors:**

```javascript
// Open browser console (F12)
// Look for service worker errors
```

**Solutions:**

1. **Verify HTTPS** - Service workers require secure context
2. **Check browser support** - Update to latest browser version
3. **Clear site data** - Browser settings → Clear site data
4. **Hard reload** - Ctrl+Shift+R (Windows) or Cmd+Shift+R (Mac)

### Offline Features Not Working

**Verify:**

1. Service worker is registered (check DevTools → Application)
2. Assets are cached (check DevTools → Application → Cache Storage)
3. You've visited the site at least once while online

**Solutions:**

- Reload the page while online
- Clear cache and reload
- Uninstall and reinstall PWA

## Best Practices

### For Mobile Recording

1. **Keep app visible** - Don't minimize, lock screen, or switch apps
2. **Keep phone plugged in** for long recordings
3. **Close unnecessary apps** to free memory
4. **Avoid taking calls** during recording (will pause)
5. **For long meetings** - Consider using native recorder app instead

### For Optimal Performance

1. **Install as PWA** for better performance
2. **Keep browser updated** for latest features
3. **Clear old recordings** to free space
4. **Monitor storage** in device settings

### Privacy Considerations

1. **Microphone access** - Only granted during recording
2. **Notifications** - Only shown during recording (mobile)
3. **Cached data** - Stored locally on your device
4. **Service worker** - Can be unregistered in browser settings

## Uninstalling the PWA

### Android

1. Long-press the app icon on home screen
2. Select **"App info"** or drag to **"Remove"**
3. Tap **"Uninstall"**

### iOS

1. Long-press the app icon
2. Select **"Remove App"**
3. Confirm **"Delete App"**

### Desktop

1. Open installed app
2. Click three-dot menu
3. Select **"Uninstall Speakr"**

Alternatively, remove from:
- **Chrome**: `chrome://apps` → Right-click app → Remove
- **Edge**: `edge://apps` → Click ⋮ on app → Uninstall
---

Have questions about PWA features? Check the [FAQ](../faq.md) or [Troubleshooting Guide](../troubleshooting.md).
