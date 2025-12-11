# Mac App Integration Guide

## Overview

Your Mac app needs to:
1. Generate a **persistent device ID** (UUID) once on first launch
2. Register with the cloud server (sends your ID, gets a token back)
3. Generate a QR code for the phone (using your persistent ID)
4. Poll the server every second for commands
5. **Self-heal** when the server restarts (re-register with the SAME ID)

**Key Design**: The Mac generates its own `device_id` and keeps it forever. This means the QR code URL never changes, even if the server restarts!

---

## Step 1: Generate Persistent Device ID (One Time Only)

On **first app launch**, generate a UUID and save it permanently:

```swift
import Foundation

class DeviceIdentity {
    private static let deviceIdKey = "la_device_id"

    /// Get or create the persistent device ID
    static func getDeviceId() -> String {
        // Check if we already have one
        if let existingId = KeychainHelper.get(key: deviceIdKey) {
            return existingId
        }

        // Generate new UUID (only happens once, ever)
        let newId = UUID().uuidString.lowercased()
        KeychainHelper.save(key: deviceIdKey, value: newId)
        return newId
    }

    /// The device ID never changes, so the QR URL is stable
    static var deviceId: String {
        return getDeviceId()
    }
}
```

**Important**: This ID is created once and never changes. Store it in Keychain so it survives app reinstalls.

---

## Step 2: Registration

When the user sets up Phone Remote (or when re-registering after server restart):

### 2.1 Hash the Emergency PIN

```swift
import CryptoKit

func hashPIN(_ pin: String) -> String {
    let data = Data(pin.utf8)
    let hash = SHA256.hash(data: data)
    return hash.compactMap { String(format: "%02x", $0) }.joined()
}
```

### 2.2 Call POST /register

**Request:**
```
POST https://la-server-xxxx.onrender.com/register
Content-Type: application/json

{
    "device_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "pin_hash": "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4"
}
```

**Response:**
```json
{
    "device_token": "xK9mN2pQ5rT8vW1y..."
}
```

### 2.3 Save the Token

```swift
// Save token (this may change on re-registration, that's OK)
KeychainHelper.save(key: "la_device_token", value: deviceToken)

// Also save PIN hash for automatic re-registration
KeychainHelper.save(key: "la_pin_hash", value: pinHash)
```

---

## Step 3: Generate QR Code for Phone

The QR code URL uses your **persistent device ID**, so it never needs to change:

```swift
func getQRCodeURL() -> String {
    let deviceId = DeviceIdentity.deviceId  // Always the same!
    return "https://YOUR-REMOTE-PAGE.com/?device=\(deviceId)"
}

// Example result (this URL is stable forever):
// https://yourusername.github.io/la-remote/?device=a1b2c3d4-e5f6-7890-abcd-ef1234567890
```

### Display as QR Code

```swift
import CoreImage.CIFilterBuiltins

func generateQRCode(from string: String) -> NSImage? {
    let context = CIContext()
    let filter = CIFilter.qrCodeGenerator()
    filter.message = Data(string.utf8)

    guard let outputImage = filter.outputImage else { return nil }

    let transform = CGAffineTransform(scaleX: 10, y: 10)
    let scaledImage = outputImage.transformed(by: transform)

    guard let cgImage = context.createCGImage(scaledImage, from: scaledImage.extent) else {
        return nil
    }

    return NSImage(cgImage: cgImage, size: NSSize(width: 200, height: 200))
}
```

---

## Step 4: Background Polling Loop (with Self-Healing)

### 4.1 The Cloud Remote Manager

```swift
import Foundation
import CryptoKit

class CloudRemoteManager {
    static let shared = CloudRemoteManager()

    private let serverURL = "https://la-server-xxxx.onrender.com"
    private let remotePageURL = "https://yourusername.github.io/la-remote/"

    private var pollTimer: Timer?
    private var isReRegistering = false

    // Callbacks for your app
    var onArmCommand: (() -> Void)?
    var onDisarmCommand: (() -> Void)?
    var onConnectionRestored: (() -> Void)?  // Called after successful re-registration

    // MARK: - Device ID (Persistent)

    private var deviceId: String {
        return DeviceIdentity.deviceId  // Never changes!
    }

    // MARK: - Registration

    func register(pin: String, completion: @escaping (Bool) -> Void) {
        let pinHash = hashPIN(pin)

        // Save PIN hash for re-registration
        KeychainHelper.save(key: "la_pin_hash", value: pinHash)

        performRegistration(pinHash: pinHash, completion: completion)
    }

    private func performRegistration(pinHash: String, completion: ((Bool) -> Void)? = nil) {
        let url = URL(string: "\(serverURL)/register")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        // Send OUR device_id (client-generated, persistent)
        let body: [String: String] = [
            "device_id": deviceId,
            "pin_hash": pinHash
        ]
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)

        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            guard let self = self,
                  let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: String],
                  let deviceToken = json["device_token"] else {
                DispatchQueue.main.async { completion?(false) }
                return
            }

            // Save the new token (token may change, that's fine)
            KeychainHelper.save(key: "la_device_token", value: deviceToken)

            DispatchQueue.main.async {
                self.isReRegistering = false
                completion?(true)
            }
        }.resume()
    }

    // MARK: - Polling

    func startPolling() {
        stopPolling()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.pollForCommands()
        }
    }

    func stopPolling() {
        pollTimer?.invalidate()
        pollTimer = nil
    }

    private func pollForCommands() {
        guard !isReRegistering,
              let deviceToken = KeychainHelper.get(key: "la_device_token") else {
            return
        }

        let url = URL(string: "\(serverURL)/poll")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "device_id": deviceId,
            "device_token": deviceToken
        ])

        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            guard let self = self else { return }

            if let httpResponse = response as? HTTPURLResponse {
                switch httpResponse.statusCode {
                case 200:
                    // Success - check for command
                    if let data = data,
                       let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                       let command = json["command"] as? String {
                        DispatchQueue.main.async {
                            switch command {
                            case "ARM":
                                self.onArmCommand?()
                            case "DISARM":
                                self.onDisarmCommand?()
                            default:
                                break
                            }
                        }
                    }

                case 404, 403:
                    // Server restarted or token expired
                    // Re-register with the SAME device_id (QR code stays valid!)
                    self.handleServerRestart()

                default:
                    break
                }
            }
        }.resume()
    }

    // MARK: - Self-Healing

    private func handleServerRestart() {
        guard !isReRegistering else { return }
        isReRegistering = true

        print("[CloudRemote] Server restart detected. Re-registering with same device ID...")

        guard let pinHash = KeychainHelper.get(key: "la_pin_hash") else {
            print("[CloudRemote] No PIN hash saved. User must set up Phone Remote again.")
            isReRegistering = false
            return
        }

        performRegistration(pinHash: pinHash) { [weak self] success in
            if success {
                print("[CloudRemote] Re-registration successful. Phone QR code still works!")
                // No need to update QR code - device_id hasn't changed!
                DispatchQueue.main.async {
                    self?.onConnectionRestored?()
                }
            } else {
                print("[CloudRemote] Re-registration failed. Will retry on next poll.")
                self?.isReRegistering = false
            }
        }
    }

    // MARK: - Helpers

    private func hashPIN(_ pin: String) -> String {
        let data = Data(pin.utf8)
        let hash = SHA256.hash(data: data)
        return hash.compactMap { String(format: "%02x", $0) }.joined()
    }

    func getQRCodeURL() -> String {
        // Device ID is persistent, so this URL never changes!
        return "\(remotePageURL)?device=\(deviceId)"
    }

    var isRegistered: Bool {
        return KeychainHelper.get(key: "la_device_token") != nil
    }
}
```

### 4.2 Using the Manager

```swift
func setupCloudRemote() {
    let manager = CloudRemoteManager.shared

    manager.onArmCommand = { [weak self] in
        self?.armAlarm()
    }

    manager.onDisarmCommand = { [weak self] in
        self?.disarmAlarm()
    }

    // Optional: notify user that connection was restored
    manager.onConnectionRestored = {
        print("Phone Remote reconnected automatically!")
        // No action needed - QR code is still valid
    }

    if manager.isRegistered {
        manager.startPolling()
    }
}

func onPhoneRemoteSetup(pin: String) {
    CloudRemoteManager.shared.register(pin: pin) { success in
        if success {
            // Show QR code (this URL is permanent!)
            let url = CloudRemoteManager.shared.getQRCodeURL()
            self.showQRCode(url: url)
            CloudRemoteManager.shared.startPolling()
        } else {
            self.showError("Failed to connect to server")
        }
    }
}
```

---

## Flow Diagram (with Persistent ID)

```
┌─────────────────────────────────────────────────────────────────┐
│                    FIRST LAUNCH (One Time)                      │
├─────────────────────────────────────────────────────────────────┤
│  Mac App                                                        │
│    │                                                            │
│    │  Generate UUID: "a1b2c3d4-e5f6-7890-..."                   │
│    │  Save to Keychain (permanent)                              │
│    ▼                                                            │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                    PHONE REMOTE SETUP                           │
├─────────────────────────────────────────────────────────────────┤
│  Mac App                                    Server              │
│    │                                          │                 │
│    │  POST /register                          │                 │
│    │  { "device_id": "a1b2...", "pin_hash": "..." }             │
│    │ ──────────────────────────────────────►  │                 │
│    │                                          │                 │
│    │  { "device_token": "xyz..." }            │                 │
│    │ ◄──────────────────────────────────────  │                 │
│    │                                          │                 │
│    │  Save token to Keychain                  │                 │
│    │  Generate QR: .../?device=a1b2...        │                 │
│    ▼                                          │                 │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│              SELF-HEALING (Server Restart)                      │
├─────────────────────────────────────────────────────────────────┤
│  Mac App                                    Server              │
│    │                                          │  (restarted)    │
│    │  POST /poll                              │                 │
│    │ ──────────────────────────────────────►  │                 │
│    │                                          │                 │
│    │  404 Not Found                           │                 │
│    │ ◄──────────────────────────────────────  │                 │
│    │                                          │                 │
│    │  POST /register (SAME device_id!)        │                 │
│    │  { "device_id": "a1b2...", "pin_hash": "..." }             │
│    │ ──────────────────────────────────────►  │                 │
│    │                                          │                 │
│    │  { "device_token": "new_xyz..." }        │                 │
│    │ ◄──────────────────────────────────────  │                 │
│    │                                          │                 │
│    │  Save new token, resume polling          │                 │
│    │  QR CODE STILL WORKS! No user action!    │                 │
│    ▼                                          │                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## JSON Shapes Summary

### POST /register
```json
// Request (Mac sends its persistent ID)
{
    "device_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "pin_hash": "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4"
}

// Response (Server returns session token)
{
    "device_token": "xK9mN2pQ5rT8vW1y..."
}
```

### POST /command
```json
// Request (Phone sends command)
{
    "device_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "pin_hash": "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4",
    "command": "ARM"
}

// Response
{"status": "ok"}
```

### POST /poll
```json
// Request
{
    "device_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "device_token": "xK9mN2pQ5rT8vW1y..."
}

// Response
{"command": "ARM"}  // or {"command": null}
```

---

## Why This is Better

| Before (Server-Generated ID) | After (Client-Generated ID) |
|------------------------------|------------------------------|
| Server restart = new device_id | Server restart = same device_id |
| QR code becomes invalid | QR code stays valid forever |
| User must re-scan QR | No user action needed |
| Frustrating on free tier | Seamless experience |

The phone's saved QR code/URL will continue to work even after the Render server sleeps and wakes up. The only thing that changes is the `device_token`, which the phone never sees anyway.
