# Chapter 3: Authentication (`adb_auth`)

Welcome back! In [Chapter 2: Transport (`atransport`)](02_transport___atransport__.md), we saw how `atransport` acts as a manager for each device connection, tracking its state and using the underlying [Connection (`Connection`/`BlockingConnection`)](01_connection___connection___blockingconnection__.md) layer to send and receive raw data packets (`apacket`s).

But how does your phone know it can *trust* the computer it's connected to? And how does the computer prove it's the same one you previously authorized? We don't want just *any* computer to be able to connect and control your phone! This is where authentication comes in.

## Motivation: The Digital Handshake

Imagine your phone is like a secure building, and your computer wants to enter. The first time your computer tries to connect (like knocking on the door), the phone doesn't recognize it. It asks the computer for some identification. If you, the owner of the phone, say "Yes, I trust this computer", the phone adds the computer's ID to its guest list.

The next time the same computer connects, the phone asks for its ID again. The computer shows its ID, the phone checks its guest list, sees the ID is on the list, and lets the computer in without bothering you again.

ADB authentication (`adb_auth`) is this security mechanism. It's a digital handshake using cryptography to ensure only authorized computers can talk to your device.

## Key Concepts

### 1. Public/Private Keys: The Secret Decoder Ring

At the heart of ADB authentication is **public/key cryptography**. It's a bit like having a special pair of keys:

*   **Private Key:** Like a secret decoder ring that only *you* have. You keep it safe on your computer (`adbkey` file). You use it to "sign" messages, proving they came from you.
*   **Public Key:** Like a special lock that only your private key can open. You can share this public key freely. Your phone stores the public keys of computers you trust (`adb_keys` file).

Anyone can use your public key to lock a message (or check a signature), but only you, with your secret private key, can unlock it (or create the signature).

### 2. The Challenge-Response Flow

When your computer (ADB Host) first connects to your phone (Device `adbd`), this happens:

1.  **Connection:** The connection is established (as seen in Chapter 1 & 2). The device `atransport` starts in a `kCsConnecting` or similar state.
2.  **Challenge (AUTH TOKEN):** The device doesn't trust the host yet. It generates a random, unique piece of data called a **token**. Think of it as the phone saying, "Prove who you are by signing this specific random number: 12345". It sends this token to the host inside an `A_AUTH` packet with type `ADB_AUTH_TOKEN`. The device transport state might move to `kCsAuthorizing`.

    ```
    Device -> Host:  A_AUTH (Type=TOKEN, Data=RandomToken)
    ```

3.  **Response (AUTH SIGNATURE):** The host receives the token. It looks for its private keys (usually in `$HOME/.android/adbkey` or paths specified by `ADB_VENDOR_KEYS`). It uses one of its private keys to "sign" the token. Signing essentially creates a unique cryptographic fingerprint of the token using the private key. The host sends this signature back to the device in an `A_AUTH` packet with type `ADB_AUTH_SIGNATURE`.

    ```
    Host -> Device:  A_AUTH (Type=SIGNATURE, Data=SignedToken)
    ```

4.  **Verification:** The device receives the signature. It looks up the public keys it has stored in its "guest list" file (`/data/misc/adb/adb_keys`). It tries each authorized public key to see if it can successfully verify the signature against the original token it sent.
    *   **Success:** If a public key successfully verifies the signature, the device knows the host has the corresponding private key and is therefore trusted. The device marks the connection as `kCsOnline` (or `kCsDevice`) and sends a `CNXN` packet confirming the connection is fully established. Game on!
    *   **Failure:** If *none* of the stored public keys work, the device knows it doesn't trust this host *yet*.

5.  **First Time / New Key (AUTH RSAPUBLICKEY):** If signature verification fails (maybe it's the first time this computer has connected, or the host tried a key the device didn't recognize), the host might try another private key (if it has more) by repeating steps 3 & 4 with a new signature. If the host runs out of private keys to try, it sends its *public key* to the device in an `A_AUTH` packet with type `ADB_AUTH_RSAPUBLICKEY`.

    ```
    Host -> Device:  A_AUTH (Type=RSAPUBLICKEY, Data=HostPublicKey)
    ```

6.  **User Confirmation:** When the device receives a public key it doesn't recognize, it triggers that familiar pop-up on your phone screen: "Allow USB debugging? The computer's RSA key fingerprint is: ...".
    *   **You tap "Allow":** The phone adds the host's public key to its `/data/misc/adb/adb_keys` file. The device marks the connection as authorized (`kCsOnline`) and sends the final `CNXN` packet.
    *   **You tap "Deny":** The connection remains unauthorized (`kCsUnauthorized`).
    *   **You check "Always allow":** The key is added, and you won't be prompted for *this specific computer* again.

This whole process ensures that only computers whose public keys are present in the device's `adb_keys` file (because you explicitly allowed them) can establish a fully functional ADB connection.

## How It Works: The Authentication Handshake

Let's visualize the successful authentication flow for a computer the phone already trusts:

```mermaid
sequenceDiagram
    participant Host as ADB Host (Computer)
    participant Device as adbd (Phone)

    Note over Host, Device: Initial TCP/USB Connection Established (Chapter 1 & 2)
    Device->>Host: Send AUTH (Type=TOKEN, Data=RandomToken)
    Note left of Device: Device state: kCsAuthorizing
    Host->>Host: Find private key (e.g., adbkey)
    Host->>Host: Sign Token using private key
    Host->>Device: Send AUTH (Type=SIGNATURE, Data=SignedToken)
    Device->>Device: Load authorized public keys (adb_keys)
    Device->>Device: Verify Signature using public keys
    alt Signature Matches a Key
        Note right of Device: Verification Successful!
        Device->>Host: Send CNXN (Connection Confirmation)
        Note left of Device: Device state: kCsOnline / kCsDevice
    else Signature Does Not Match Any Key
        Note right of Device: Verification Failed!
        opt Host has more keys
            Device->>Host: Send AUTH (Type=TOKEN, Data=NewRandomToken)
            Note left of Device: Retry with next key...
        else Host sends Public Key
            Host->>Device: Send AUTH (Type=RSAPUBLICKEY, Data=HostPublicKey)
            Device->>Device: Ask User: "Allow USB Debugging?"
            alt User Allows
                Device->>Device: Add Host Public Key to adb_keys
                Device->>Host: Send CNXN (Connection Confirmation)
                Note left of Device: Device state: kCsOnline / kCsDevice
            else User Denies
                 Note left of Device: Device state: kCsUnauthorized
                 Device->>Host: (Connection eventually closed or limited)
            end
        end
    end
```

## Code Walkthrough (Simplified)

Let's peek at some relevant code snippets.

**On the Device (adbd - `daemon/auth.cpp`)**

1.  **Sending the Challenge:** When a connection needs authentication, the device sends the token request.

    ```c++
    // Simplified from daemon/auth.cpp - send_auth_request
    void send_auth_request(atransport* t) {
        LOG(INFO) << "Calling send_auth_request...";

        // Generate a secure random token (like a random number)
        if (!adbd_auth_generate_token(t->token, sizeof(t->token))) {
            // Error handling...
            return;
        }

        // Create an AUTH packet
        apacket* p = get_apacket();
        p->msg.command = A_AUTH; // AUTH command
        p->msg.arg0 = ADB_AUTH_TOKEN; // Type: TOKEN
        p->msg.data_length = sizeof(t->token); // Length of the token
        // Copy the token into the packet payload
        p->payload.assign(t->token, t->token + sizeof(t->token));

        // Send the packet over the connection (using the transport layer)
        send_packet(p, t); // send_packet eventually calls t->connection()->Write()
    }
    ```
    *   `adbd_auth_generate_token`: Creates the random challenge.
    *   `get_apacket`, `p->msg.*`, `p->payload`: Building the `apacket` structure (See [Chapter 4: ADB Protocol & Messaging](04_adb_protocol___messaging.md)).
    *   `send_packet`: Sends the packet down through the [Transport (`atransport`)](02_transport___atransport__.md) and [Connection (`Connection`/`BlockingConnection`)](01_connection___connection___blockingconnection__.md) layers.

2.  **Verifying the Signature:** When an `AUTH SIGNATURE` packet arrives (`handle_packet` in `adb.cpp` calls this).

    ```c++
    // Simplified from daemon/auth.cpp - adbd_auth_verify
    bool adbd_auth_verify(const char* token, size_t token_size, const std::string& sig) {
        // Paths where authorized public keys are stored on the device
        static constexpr const char* key_paths[] = { "/adb_keys", "/data/misc/adb/adb_keys", nullptr };

        for (const auto& path : key_paths) {
            // Check if the key file exists and is readable
            if (access(path, R_OK) == 0) {
                LOG(INFO) << "Loading keys from " << path;
                // Read the content of the file (contains multiple public keys, one per line)
                std::string content;
                if (!android::base::ReadFileToString(path, &content)) continue;

                // Try each key listed in the file
                for (const auto& line : android::base::Split(content, "\n")) {
                    // 1. Decode the Base64 public key from the line
                    // 2. Convert the decoded key into an RSA public key structure
                    // ... (details skipped for simplicity) ...
                    RSA* key = /* ... decoded RSA public key ... */;
                    if (!key) continue;

                    // 3. *** THE CORE VERIFICATION STEP ***
                    // Use OpenSSL to check if the signature 'sig' is valid for the
                    // original 'token' using the current public 'key'.
                    bool verified =
                        (RSA_verify(NID_sha1, /* token data */, token_size,
                                    /* signature data */, sig.size(),
                                    key) == 1);
                    RSA_free(key); // Clean up

                    if (verified) {
                         LOG(INFO) << "Signature verification success with key from " << path;
                         return true; // Found a matching key! Host is trusted.
                    }
                }
            }
        }
        LOG(WARNING) << "Signature verification failed, unknown key";
        return false; // No key in any file matched the signature.
    }
    ```
    *   It reads the `/data/misc/adb/adb_keys` file (or `/adb_keys`).
    *   For each public key listed, it uses `RSA_verify` to check if the received signature matches the token.

3.  **Handling a New Public Key:** When an `AUTH RSAPUBLICKEY` arrives.

    ```c++
    // Simplified from daemon/auth.cpp - adbd_auth_confirm_key
    void adbd_auth_confirm_key(const char* key_data, size_t len, atransport* t) {
        // This function doesn't actually show the UI popup itself.
        // It sends the received public key ('key_data') over a separate
        // communication channel (a Unix domain socket) to a system
        // service/daemon on Android that is responsible for showing the
        // confirmation dialog to the user.

        LOG(DEBUG) << "Received public key, forwarding to system for confirmation...";

        // Simplified: Assume 'framework_fd' is the socket to the confirmation UI service
        if (framework_fd >= 0) {
             // Format message "PK<base64_public_key>"
             char msg[MAX_PAYLOAD_V1];
             snprintf(msg, sizeof(msg), "PK%s", key_data);
             // Send the public key to the confirmation service
             unix_write(framework_fd, msg, strlen(msg));
        } else {
             LOG(ERROR) << "Framework confirmation service not connected.";
             // Need to retry later if the service connects
             needs_retry = true;
        }
        // Now we wait for the system service to respond back (via adbd_auth_event)
        // telling us if the user tapped "Allow" or "Deny".
    }
    ```
    *   This code *doesn't* show the pop-up directly. It sends the key to another part of the Android system responsible for the UI.
    *   If the user taps "Allow", that system service will eventually add the key to `/data/misc/adb/adb_keys` and notify `adbd` (via `adbd_auth_event`), which then calls `adbd_auth_verified` to mark the transport online.

**On the Host (adb - `client/auth.cpp`)**

1.  **Loading Private Keys:** Happens during ADB server startup.

    ```c++
    // Simplified from client/auth.cpp - adb_auth_init / load_keys
    void adb_auth_init() {
        LOG(INFO) << "adb_auth_init...";

        // 1. Find/Generate the user's primary key file
        // Usually $HOME/.android/adbkey
        std::string user_key_path = get_user_key_path();
        if (/* key file doesn't exist */) {
            generate_key(user_key_path); // Create a new private/public key pair
        }
        load_key(user_key_path); // Load the private key into memory (g_keys map)

        // 2. Load any vendor keys specified by environment variable
        const auto& vendor_key_paths = get_vendor_keys(); // Gets paths from ADB_VENDOR_KEYS
        for (const std::string& path : vendor_key_paths) {
            load_keys(path); // Load keys from specified files/directories
        }
        // Keys are stored in the global 'g_keys' map, indexed by a hash of the public key.
    }

    // Helper to load a single key file
    static bool load_key(const std::string& file) {
        std::shared_ptr<RSA> key = read_key_file(file); // Reads PEM formatted private key
        if (!key) return false;

        // Store the loaded key (std::shared_ptr<RSA>) in a global map
        std::lock_guard<std::mutex> lock(g_keys_mutex);
        std::string fingerprint = hash_key(key.get()); // Calculate unique ID for the key
        g_keys[fingerprint] = std::move(key);
        return true;
    }
    ```
    *   `adb_auth_init`: Called when the ADB server starts.
    *   `get_user_key_path`: Finds `$HOME/.android/adbkey`.
    *   `generate_key`: Creates `adbkey` and `adbkey.pub` if they don't exist.
    *   `load_key`: Reads the private key from a file using `PEM_read_RSAPrivateKey` and stores it in a global map `g_keys`.
    *   `get_vendor_keys`: Checks the `ADB_VENDOR_KEYS` environment variable for more key locations.

2.  **Signing the Token and Sending Response:** When an `AUTH TOKEN` arrives (`handle_packet` in `adb.cpp` calls this).

    ```c++
    // Simplified from client/auth.cpp - send_auth_response
    void send_auth_response(const char* token, size_t token_size, atransport* t) {
        // Get the next available private key associated with this transport to try.
        // The transport 't' keeps track of which keys it has already tried.
        std::shared_ptr<RSA> key = t->NextKey();

        if (key == nullptr) {
            // We've tried all our private keys, none worked.
            // Send our public key instead to prompt the user on the device.
            LOG(INFO) << "No more private keys to try, sending public key";
            t->SetConnectionState(kCsUnauthorized); // Mark as unauthorized for now
            send_auth_publickey(t); // Send AUTH(RSAPUBLICKEY)
            return;
        }

        LOG(INFO) << "Signing token with a private key...";
        // Use OpenSSL's RSA_sign to create the signature
        std::string signature = adb_auth_sign(key.get(), token, token_size);
        if (signature.empty()) {
            // Error handling...
            return;
        }

        // Create the AUTH packet for the signature
        apacket* p = get_apacket();
        p->msg.command = A_AUTH;
        p->msg.arg0 = ADB_AUTH_SIGNATURE; // Type: SIGNATURE
        p->payload.assign(signature.begin(), signature.end()); // Payload is the signature
        p->msg.data_length = p->payload.size();

        // Send the packet
        send_packet(p, t);
    }
    ```
    *   `t->NextKey()`: Gets the next private key from the loaded `g_keys` that hasn't been tried yet for this specific connection attempt.
    *   `adb_auth_sign`: Uses `RSA_sign` with the chosen private key and the received token to generate the signature.
    *   `send_auth_publickey`: If `NextKey` returns `nullptr` (no more keys), this function sends the default public key (`adbkey.pub`) instead.

## Implementation Details

*   **Protocol:** The authentication messages (`A_AUTH`) are defined in `protocol.txt`. The `arg0` field indicates the type (`ADB_AUTH_TOKEN`, `ADB_AUTH_SIGNATURE`, `ADB_AUTH_RSAPUBLICKEY`) and the `payload` carries the token, signature, or public key data.
*   **State Management:** The `atransport` object (Chapter 2) tracks the authentication state (`kCsConnecting`, `kCsAuthorizing`, `kCsUnauthorized`, `kCsOnline`).
*   **Cryptography:** OpenSSL library functions (`RSA_sign`, `RSA_verify`, `PEM_read_RSAPrivateKey`, etc.) are used for the cryptographic operations.
*   **Key Storage:**
    *   Device: `/data/misc/adb/adb_keys` (plain text file, one Base64 encoded public key per line).
    *   Host: `$HOME/.android/adbkey` (PEM-encoded private key), `$HOME/.android/adbkey.pub` (Base64 public key + user info), `ADB_VENDOR_KEYS` environment variable can point to other key files or directories.

## Conclusion

ADB authentication (`adb_auth`) is a crucial security layer built upon public/private key cryptography. It ensures your device only talks to computers you have explicitly trusted via a challenge-response mechanism.

1.  The device challenges the host with a random **token**.
2.  The host **signs** the token using its **private key**.
3.  The device **verifies** the signature using its list of authorized **public keys**.
4.  If the host's key isn't known, the host sends its **public key**, and the device prompts the user for confirmation.

This digital handshake protects your device from unauthorized access.

With the connection established and authenticated, we can finally look at the actual messages ADB uses to get things done.

**Next:** [Chapter 4: ADB Protocol & Messaging](04_adb_protocol___messaging.md)

---

Generated by [AI Codebase Knowledge Builder](https://github.com/The-Pocket/Tutorial-Codebase-Knowledge)