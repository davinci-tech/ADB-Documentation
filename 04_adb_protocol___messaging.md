# Chapter 4: ADB Protocol & Messaging

Welcome back! In [Chapter 3: Authentication (`adb_auth`)](03_authentication___adb_auth__.md), we saw how ADB uses a secure digital handshake to make sure your computer and device trust each other before allowing full communication.

Now that the connection is established and trusted, how do the different parts of ADB (the client on your computer, the server on your computer, and the daemon on your device) actually *talk* to each other? What format do their messages take?

## Motivation: Speaking the Same Language

Imagine you have three people who need to work together:
1.  **Client:** The `adb` command you type in your terminal.
2.  **Server:** A background program on your computer that manages connections.
3.  **Daemon (`adbd`):** A background program on your Android device.

They need to send instructions and data back and forth ("list devices", "install this app", "here's the output from that command"). If they all spoke different languages or used different ways of addressing letters, it would be chaos!

The **ADB Protocol & Messaging** layer defines the **standard language and envelope format** that all ADB components *must* use. It's like agreeing that all mail within the ADB system will use a specific envelope size, specific fields on the envelope (like "To:", "From:", "Subject:"), and a specific language inside the letter. This ensures everyone understands the messages, no matter if they are sent over a USB cable or a Wi-Fi network ([Connection (`Connection`/`BlockingConnection`)](01_connection___connection___blockingconnection__.md)).

## Key Concepts

### 1. The ADB Packet (`apacket`): The Envelope + Letter

The fundamental unit of communication in ADB is the **packet**, represented by the `apacket` structure (defined in `types.h`). Think of it as the complete mail package: the envelope and the letter inside.

```c++
// Simplified from types.h
struct apacket {
    amessage msg;          // The "envelope" (header)
    Block    payload;      // The "letter" (data, if any)
};
```

*   **`msg` (`amessage`):** This is the header, like the outside of an envelope. It contains instructions and information *about* the message. It's always a fixed size (24 bytes).
*   **`payload` (`Block`):** This is the actual data being sent, like the letter inside the envelope. It can be empty, or it can contain up to a certain maximum size (defined during the connection setup). `Block` is a custom type in `adb_codebase` similar to `std::vector<char>` used to hold raw bytes.

Every single piece of information exchanged between ADB components is wrapped in one of these `apacket` structures.

### 2. The Message Header (`amessage`): The Envelope Details

The `amessage` structure is the heart of the protocol. It's a 24-byte header with six 32-bit fields, defining what the message is about and how to handle the payload.

```c++
// Simplified from types.h
struct amessage {
    uint32_t command;     // What action to perform (e.g., A_CNXN, A_WRTE)
    uint32_t arg0;        // First argument (meaning depends on command)
    uint32_t arg1;        // Second argument (meaning depends on command)
    uint32_t data_length; // How many bytes are in the payload
    uint32_t data_check;  // Checksum of the payload data (to detect corruption)
    uint32_t magic;       // command XOR 0xffffffff (simple header check)
};
```

Let's break down these fields like reading an envelope:

*   **`command`:** The most important field! This is like the "Subject" line or instruction on the envelope. It's a unique code (like `A_WRTE` for "Write" or `A_OPEN` for "Open a new connection") telling the receiver what kind of message this is and what to do. These commands are defined as constants (e.g., in `adb.h`).
*   **`arg0`, `arg1`:** These are like extra instructions or addresses on the envelope. Their meaning depends entirely on the `command`. For example, for `A_OPEN`, `arg0` might be a temporary ID for the connection the sender is trying to create. For `A_WRTE`, they might be IDs identifying the specific stream the data belongs to.
*   **`data_length`:** Tells the receiver exactly how many bytes of data to expect in the `payload` (the "letter"). If this is 0, there's no payload attached.
*   **`data_check`:** A checksum calculated from the `payload` data. The receiver recalculates this checksum and compares it. If they don't match, the data got corrupted during transmission (like the letter getting smudged). *Note: Newer ADB versions might skip this check for performance.*
*   **`magic`:** A simple sanity check for the header itself. It's calculated by taking the `command` and XORing it with `0xffffffff`. The receiver checks if `command XOR magic == 0xffffffff`. If not, the header itself is likely corrupt.

### 3. The Payload (`apacket.payload`): The Letter Inside

This is just a block of raw bytes. Its content depends entirely on the `command` in the header.

*   If the `command` is `A_WRTE` (Write), the payload contains the actual data being sent (e.g., shell command output, file data).
*   If the `command` is `A_OPEN` (Open), the payload contains the name of the service the sender wants to connect to (e.g., `"shell:ls -l"` or `"sync:"`).
*   If the `command` is `A_CNXN` (Connect), the payload contains information about the system connecting (like "device", "host", supported features).
*   For commands like `A_OKAY` (Ready/Acknowledge) or `A_CLSE` (Close), `data_length` is usually 0, and the payload is empty.

### 4. The Commands: The ADB Language

The `command` field defines the action. Here are some fundamental commands defined in `adb.h` and `protocol.txt`:

*   **`A_CNXN` (Connect):** The first message sent by both sides after a connection is physically established. Used to negotiate protocol version, maximum payload size, and exchange system identity.
*   **`A_AUTH` (Authenticate):** Used during the authentication handshake (see [Chapter 3](03_authentication___adb_auth__.md)). `arg0` specifies the type (Token, Signature, Public Key), and the payload contains the relevant data.
*   **`A_OPEN` (Open Stream):** Requests to open a new communication channel (a "stream") to a specific service (e.g., `shell`, `sync`). The payload contains the service name string. `arg0` is a temporary ID assigned by the sender.
*   **`A_OKAY` (Ready/Acknowledge):** Confirms readiness or acknowledges a previous message. Used heavily in stream communication. `arg0` and `arg1` usually contain stream IDs. Signals the sender that the receiver is ready for more data on a specific stream.
*   **`A_WRTE` (Write Data):** Sends actual data over an established stream. The payload contains the data. `arg0` and `arg1` identify the local and remote stream IDs.
*   **`A_CLSE` (Close Stream):** Informs the other side that a stream is being closed. `arg0` and `arg1` identify the stream IDs.

Think of these commands as the core vocabulary of the ADB protocol.

## How It Works: A Basic Exchange (Client asks Server for Version)

Let's trace a very simple interaction: the `adb` client asking the ADB server for its version (`adb version`).

1.  **Client -> Server (Smart Socket Request):** The `adb` client first connects to the ADB server running on `localhost:5037`. It sends a special preliminary request *before* using `apacket`s. This "smart socket" request is just ASCII text: `000Chost:version`. (`000C` is hex for 12, the length of `host:version`). (See `OVERVIEW.TXT` / `SERVICES.TXT`).
2.  **Server -> Client (Smart Socket Reply):** The server receives this text request. It sees it's a `host:` request (handled by the server itself). It replies with `OKAY` in ASCII to acknowledge the request.
3.  **Server -> Client (Actual Data):** The server *then* sends the actual version information back, again using the smart socket ASCII protocol: it sends a 4-byte hex length followed by the version string (e.g., `00040029` if the version is 41 - `0x29`).
4.  **Client Reads:** The client reads the `OKAY`, then reads the length, then reads the version string and displays it. The connection is then typically closed.

*Note:* This specific example (`host:version`) uses the older "smart socket" text protocol for client-server communication. *Internal* server-daemon communication, and data transfer *after* a service like `shell` or `sync` is opened, uses the binary `apacket` protocol described above.

Let's look at the `apacket` structure for internal communication.

## Code Walkthrough (Simplified)

Let's look at the structures defined in the code.

**File: `types.h`**

```c++
// The header structure (always 24 bytes)
struct amessage {
    uint32_t command;     // Command identifier (A_CNXN, A_WRTE, etc.)
    uint32_t arg0;        // First argument
    uint32_t arg1;        // Second argument
    uint32_t data_length; // Length of payload (0 is allowed)
    uint32_t data_check;  // Checksum of data payload
    uint32_t magic;       // Command ^ 0xffffffff
};

// The full packet structure (header + payload)
struct apacket {
    using payload_type = Block; // Block is like std::vector<char>
    amessage msg;              // The header instance
    payload_type payload;      // The payload data
};
```

*   These C++ structs directly map to the protocol definition. When ADB sends a message, it fills these fields and sends the raw bytes over the connection. When it receives data, it reads 24 bytes into an `amessage`, checks the `magic`, reads `data_length` bytes into the `payload`, and optionally verifies the `data_check`.

**File: `adb.cpp` (Example: Sending A_OKAY)**

```c++
// Helper function to send an A_OKAY message
static void send_ready(unsigned local_id, unsigned remote_id, atransport *t)
{
    D("Calling send_ready");
    // 1. Allocate a new packet structure
    apacket *p = get_apacket();

    // 2. Fill in the header fields
    p->msg.command = A_OKAY;    // Set the command type
    p->msg.arg0 = local_id;     // Set the first argument (local stream ID)
    p->msg.arg1 = remote_id;    // Set the second argument (remote stream ID)
    p->msg.data_length = 0;     // No payload for A_OKAY
    // 'magic' and 'data_check' will be set by send_packet just before sending

    // 3. Send the packet via the transport
    // send_packet calculates checksum/magic and calls t->connection()->Write()
    send_packet(p, t);
    // Note: send_packet takes ownership and will call put_apacket(p) later.
}
```

*   This shows how straightforward it is to create a command packet. You get an `apacket`, fill in the `amessage` fields according to the protocol rules for that command, and then call a function (`send_packet`) to handle the final details (checksum, magic) and send it down through the [Transport (`atransport`)](02_transport___atransport__.md) and [Connection (`Connection`/`BlockingConnection`)](01_connection___connection___blockingconnection__.md) layers.

**File: `adb.cpp` (Example: Receiving and Handling Packets)**

```c++
// Main packet handling function, called when a full packet arrives
// from the Connection layer's ReadCallback.
void handle_packet(apacket *p, atransport *t)
{
    // Basic logging and validation might happen here...
    print_packet("recv", p); // Optional debug printing
    CHECK_EQ(p->payload.size(), p->msg.data_length); // Sanity check

    // The core logic: decide what to do based on the command
    switch(p->msg.command){
    case A_CNXN:
        // Handle a new connection attempt
        handle_new_connection(t, p);
        break;

    case A_AUTH:
        // Handle an authentication message
        handle_auth_packet(t, p); // Fictional function for brevity
        break;

    case A_OPEN:
        // Handle a request to open a new stream to a service
        handle_open_packet(t, p); // Fictional function for brevity
        break;

    case A_OKAY:
        // Handle an acknowledgement/ready signal for a stream
        handle_okay_packet(t, p); // Fictional function for brevity
        break;

    case A_WRTE:
        // Handle incoming data for a stream
        handle_write_packet(t, p); // Fictional function for brevity
        break;

    case A_CLSE:
        // Handle a stream closure notification
        handle_close_packet(t, p); // Fictional function for brevity
        break;

    default:
        // Unknown command - likely an error, might close connection
        printf("handle_packet: what is %08x?!\n", p->msg.command);
        // Close connection or ignore? Depends on severity.
    }

    // Clean up the packet structure now that we're done with it
    put_apacket(p);
}
```

*   This `switch` statement is the central dispatcher for incoming messages. When the lower layers deliver a complete `apacket`, this function examines the `command` field and calls the appropriate handler function to process that specific type of message, using the data from `arg0`, `arg1`, and `payload` as needed.

## Why Checksums and Magic?

*   **`magic`:** This is a very simple check. If a few bits in the header get flipped during transmission (maybe due to electrical noise on USB), the `command` field might change. Checking `command ^ magic == 0xffffffff` provides a quick, though not foolproof, way to detect some header corruption.
*   **`data_check`:** This is a CRC32 checksum of the payload data. It's much more robust for detecting errors within the actual data being sent. If bytes in the payload are changed, added, or deleted, the checksum calculated by the receiver almost certainly won't match the `data_check` value sent in the header, indicating the data is bad. As mentioned, newer ADB versions (`A_VERSION_SKIP_CHECKSUM`) can optionally skip this calculation and check, relying on the underlying USB or TCP protocols to handle error detection, which can improve performance slightly.

## Conclusion

The ADB protocol, centered around the `amessage` header and the `apacket` structure, provides the essential standardized language and format for all communication within the ADB system. It defines:

*   A fixed **header** (`amessage`) with command, arguments, payload length, checksum, and magic number.
*   An optional **payload** (`apacket.payload`) carrying the actual data.
*   A set of **commands** (`A_CNXN`, `A_OPEN`, `A_WRTE`, `A_OKAY`, `A_CLSE`, etc.) that define the actions and interactions.

This consistent structure ensures that the ADB client, server, and daemon can reliably exchange information and instructions, regardless of the underlying transport (USB or TCP).

Now that we understand how individual messages are structured, how does ADB manage the *flow* of data for specific tasks, like running a shell command or transferring files? These tasks often involve multiple messages back and forth, forming logical "streams" of communication. In the next chapter, we'll look at the [Socket (`asocket`)](05_socket___asocket__.md) abstraction, which represents these logical streams built on top of the raw packet protocol.

**Next:** [Chapter 5: Socket (`asocket`)](05_socket___asocket__.md)

---

Generated by [AI Codebase Knowledge Builder](https://github.com/The-Pocket/Tutorial-Codebase-Knowledge)