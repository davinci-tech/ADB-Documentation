import click
import rich
import rich.rule
import pdb
from matchpy import *
from construct import Struct, Const, Int32ul, GreedyRange, Bytes, this, Probe, Debugger, RepeatUntil, Optional
from pathlib import Path

import scapy.all as scapy

ADBMessage = Struct(
    "command" / Bytes(4),
    "arg0" / Bytes(4),
    "arg1" / Bytes(4),
    "length" / Int32ul,
    "checksum" / Int32ul,
    "magic" / Bytes(4),
)

ADBPacket = Struct(
    "header" / ADBMessage,
    "payload" / Optional(Bytes(this.header.length))
)

GluedADBPackets = GreedyRange(ADBPacket)

class GluedPacket:
    def __init__(self, src_port, dst_port, payload):
        self.src_port = src_port
        self.dst_port = dst_port
        self.payload = payload

    def __repr__(self):
        return f"GluedPacket(src_port={self.src_port}, dst_port={self.dst_port}, payload_len={len(self.payload)})"

"""
Basically a wrapper around ADBPacket which stores the source and destination ports of the packt.
"""
class XADBPacket:
    def __init__(self, adb_packet, src_port, dst_port):
        self.adb_packet = adb_packet
        self.src_port = src_port
        self.dst_port = dst_port


def display(data):
    def is_printable(b):
        return 32 <= b < 127
    return ''.join(chr(b) if is_printable(b) else '.' for b in data)

def displayGluedPackets(gluedPackets, server_port, client_port):
    for i, packet in enumerate(gluedPackets):
        direction = "S->C "
        color = "red"
        if (packet.src_port == client_port and packet.dst_port == server_port):
            direction = "C->S "
            color = "blue"
        rich.print(f"{i} [{color}]{direction}[/] {display(packet.payload[:200])}")

def displayXADBPackets(xadbPackets, server_port, client_port):
    for i, xadbPacket in enumerate(xadbPackets):
        direction = "S->C "
        color = "red"
        if (xadbPacket.src_port == client_port and xadbPacket.dst_port == server_port):
            direction = "C->S "
            color = "blue"

        payloadInfo = f"len={xadbPacket.adb_packet.header.length} payload={display(xadbPacket.adb_packet.payload[:100])}"
        if xadbPacket.adb_packet.header.length == 0:
            payloadInfo = ""

        rich.print(f"{i} [{color}]{direction}[/] {xadbPacket.adb_packet.header.command.decode()} {payloadInfo}")




"""
This returns the TCP packets which have been used to transmit adb-related packets.
"""
def filterPackets(packets, server_port, client_port):
    for i, packet in enumerate(packets):
        if packet.haslayer(scapy.TCP) and len(packet[scapy.TCP].payload) > 0:
            sport = packet[scapy.TCP].sport
            dport = packet[scapy.TCP].dport
            if (sport == server_port or dport == server_port) and (sport == client_port or dport == client_port):
                yield packet

"""
In ADB, data can be split across multiple TCP packets (especially when sending files for a RECV command).
This function glues them back together.
"""
def glueTCPPackets(packets, server_port, client_port):
    buffer = None
    last_src_port = last_dst_port = None

    for packet in packets:
        if not packet.haslayer(scapy.TCP):
            continue
        src_port = packet[scapy.TCP].sport
        dst_port = packet[scapy.TCP].dport
        payload = bytes(packet[scapy.TCP].payload)
        if buffer is not None and src_port == last_src_port and dst_port == last_dst_port:
            buffer += payload
        else:
            if buffer is not None:
                yield GluedPacket(last_src_port, last_dst_port, buffer)
            buffer = payload
            last_src_port = src_port
            last_dst_port = dst_port

    if buffer is not None:
        yield GluedPacket(last_src_port, last_dst_port, buffer)


"""
Given a glued packet, this splits it into individual XADB packets (which also store the source and destination ports).
"""
def gluedPacket2XADBPackets(gluedPacket):
    return [XADBPacket(adbPacket, gluedPacket.src_port, gluedPacket.dst_port) for adbPacket in GluedADBPackets.parse(gluedPacket.payload)]

"""
When a file is requested via RECV, the server sends a lot of DATA commands (yes, they are called commands even
though they are from the server and their role is to send you a response) followed by a DONE. This function assumes
a payload of that format and extracts the file content from it.
"""
def extractFileFromDataCommands(payload):
    def peek(ctx, len):
        # Look at next 4 bytes without consuming
        marker = ctx._io.read(len)
        ctx._io.seek(-len, 1)   # rewind after peek
        return marker
        
    # Define the DATA and DONE command structures
    DataCommand = Debugger(Struct(
        "header" / Const(b"DATA"),
        "length" / Int32ul,
        # "payload" / RepeatUntil(lambda obj, lst, ctx: peek(ctx, 4) in [b'DATA', b'DONE'], Bytes(1))
        "payload" / Bytes(this.length)
    ))

    DoneCommand = Struct(
        "header" / Const(b"DONE"),
        "modification_time" / Bytes(4)
    )

    # Parser for a sequence of DATA commands followed by DONE
    FileTransfer = Struct(
        # "chunks" / GreedyRange(DataCommand),
        "chunks" / RepeatUntil(lambda obj, lst, ctx: peek(ctx, 4) == b'DONE', DataCommand),
        "done" / Optional(DoneCommand)
    )

    parsed = FileTransfer.parse(payload)
    
    # Concatenate all payloads from DATA commands
    ans = b''
    for chunk in parsed.chunks:
        ans += chunk.payload
    return ans


@click.command()
@click.argument('pcap_path', type=click.Path(exists=True))
@click.option('--server-port', default=5037, show_default=True, help='Port where the adb server was listening on.')
@click.option('--client-port', required=True, type=int, help='Port from where commands were sent to the adb server.')
@click.option(
    '--output-dir',
    type=click.Path(file_okay=False, dir_okay=True, writable=True, resolve_path=True),
    default=None,
    help='Directory where extracted files will be stored. Defaults to ./extracted-client{client_port}-server{server_port}'
)
def extract(pcap_path, server_port, client_port, output_dir):
    def createOutputDirIfNotExists():
        nonlocal output_dir
        # Set output directory default if not specified
        if output_dir is None:
            output_dir = Path.cwd() / f"extracted-client{client_port}-server{server_port}"
        else:
            output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

    packets = scapy.rdpcap(pcap_path)
    print(f"Server port: {server_port}")
    print(f"Client port: {client_port}")
    print()

    filteredPackets = filterPackets(packets, server_port, client_port)
    gluedPackets = list(glueTCPPackets(filteredPackets, server_port, client_port))
    xadbPackets = [xadbPacket for gp in gluedPackets for xadbPacket in gluedPacket2XADBPackets(gp)]

    rich.print(rich.rule.Rule(title="Glued Packets", style="bold green", characters="═"))
    displayGluedPackets(gluedPackets, server_port, client_port)
    print()

    rich.print(rich.rule.Rule(title="ADB Packets", style="bold green", characters="═"))
    displayXADBPackets(xadbPackets, server_port, client_port)
    print()

    """
    At this point we've successfully parsed all the ADB packets. We also added metadata related to their source and destination ports.
    From now on, we can focus on extracting files that were transferred via RECV/DATA commands.
    For those who consider reusing this code, it is enough to delete everything below and do whatever you want with the xadbPackets list
    since it is quite a useful abstraction.
    """

    # First we filter out everything that is not a WRTE packet
    packets = [xap for xap in xadbPackets if xap.adb_packet.header.command == b'WRTE']

    rich.print(rich.rule.Rule(title="Filtered XADB Packets", style="bold green", characters="═"))
    displayXADBPackets(packets, server_port, client_port)
    print()

    # Now we need to start searching for a file transfer. At least in shell file transfers, the transfer looks like this
    # (ignoring the OKAYs and other non-WRTE packets):
    #   1. The client sends a RECV command to the server (the server being the phone)
    #   2. The server begings sending DATA commands (at least 1) to the client (the computer) containing the actual file and it finishes with a DONE command
    # Basically, it will look something like this:
    #   C->S  WRTE payload=RECV....<filepath>
    #   S->C  WRTE payload=DATA....<file chunk 1>
    #   S->C  WRTE payload=....<file chunk 2>
    #   ...
    #   S->C  WRTE payload=....<file chunk N>DONE<modtime>
    # Please notice that a DATA is usually split across multiple WRTE packets (so it is perfectly normal for a WRTE payload
    # from a data transfer to not begin with the prefix 'DATA', if this happens it is purely coincidental - or the first WRTE).
    i = 0
    files_found = 0
    while i < len(packets):
        pkt = packets[i]
        # Check for C->S WRTE packet starting with RECV
        if pkt.src_port == client_port and pkt.dst_port == server_port and pkt.adb_packet.payload.startswith(b'RECV'):
            server_filepath = pkt.adb_packet.payload[8:].decode()
            raw_data = b''
            i += 1
            # Collect all following S->C WRTE packets
            while i < len(packets):
                next_pkt = packets[i]
                if next_pkt.src_port == server_port and next_pkt.dst_port == client_port:
                    raw_data += next_pkt.adb_packet.payload
                    i += 1
                else:
                    break

            if len(raw_data) > 0:
                createOutputDirIfNotExists()
                files_found += 1
                file = extractFileFromDataCommands(raw_data)
                filepath = output_dir / f"file{files_found}.apk"
                print(f"Found file transfer for '{server_filepath}' with length of {len(raw_data)} bytes. Writing to {filepath}")
                with open(filepath, "wb") as f:
                    f.write(file)
        else:
            i += 1




if __name__ == '__main__':
    extract()