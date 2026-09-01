"""photon-lite -- a minimal Photon LoadBalancing server for Dragalia Lost 2.19.0 co-op on a Dawnshard private server.

One process replaces the (proprietary, licence-gated) Photon Server + the Dawnshard Photon plugin + the Photon State
Manager for single-player co-op rooms: it speaks the client's ENet/UDP transport (CRC-32 headers, reliable commands,
fragments), Init, Photon's Diffie-Hellman/AES encryption, Protocol16, the LoadBalancing master/game operations, and
ports the plugin's room logic (GoToIngameState 1-4, heroparam/batch, Party/CharacterData, Ready->StartQuest,
ClearQuestRequest->dungeon_record/record_multi) plus the State-Manager HTTP API the Dawnshard container queries.

    python photon_lite.py --lan-ip 192.168.1.10 [--api http://127.0.0.1:5000] [--token photon-lite-token]
                          [--godmode] [--atkbuff] [--fill 4] [--no-ghost] [--port 5055] [--state-port 5057]

Dawnshard (docker-compose environment):
    PhotonOptions__ServerUrl=<lan-ip>:5055
    PhotonOptions__Token=<token>
    PhotonOptions__StateManagerUrl=http://host.docker.internal:5057

Protocol facts were read from the 2.19.0 iOS client binary and verified on the wire; the room logic follows
Dawnshard's PhotonPlugin (MIT). Not affiliated with Exit Games / Photon, Cygames or Nintendo.
"""
import argparse, collections, hashlib, os, random, socket, struct, sys, threading, time, datetime, zlib, traceback, re, json
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import msgpack, lz4.block
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HERE = os.path.dirname(os.path.abspath(__file__))


def _detect_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except OSError:
        return "127.0.0.1"


_ap = argparse.ArgumentParser(description="photon-lite: Photon LoadBalancing replacement for Dragalia Lost co-op (Dawnshard)")
_ap.add_argument("--lan-ip", default=None, help="this PC's LAN address as the phone sees it (default: auto-detect)")
_ap.add_argument("--port", type=int, default=5055, help="master UDP port (= PhotonOptions__ServerUrl port)")
_ap.add_argument("--game-port", type=int, default=None, help="game-server UDP port (default: port+1)")
_ap.add_argument("--state-port", type=int, default=None, help="State-Manager HTTP port (default: port+2)")
_ap.add_argument("--api", default="http://127.0.0.1:5000", help="Dawnshard API base URL as reachable from this PC")
_ap.add_argument("--api-prefix", default="2.19.0_20220714193707", help="Dawnshard route prefix (Android or iOS one, both accepted)")
_ap.add_argument("--token", default="photon-lite-token", help="bearer for dungeon_record/record_multi (= PhotonOptions__Token)")
_ap.add_argument("--fill", type=int, default=4, help="units a lone player controls in a NORMAL co-op room (Dawnshard rule = 3)")
_ap.add_argument("--no-ghost", action="store_true", help="do not seat the ghost second player (start button stays grey alone)")
_ap.add_argument("--godmode", action="store_true", help="full heal 5x/s + damage shield + damage-cut buffs on every unit")
_ap.add_argument("--atkbuff", action="store_true", help="+225%% attack buffs (curse-of-emptiness immune)")
_ap.add_argument("--cleanse", action="store_true", help="remove every debuff an enemy puts on your units (implied by --godmode)")
_ap.add_argument("--no-cleanse", action="store_true", help="disable the cleanse even with --godmode (A/B testing)")
_ap.add_argument("--log-buffs", action="store_true", help="decode-log every ChangeBuff event (multi-KB lines; off by default since 2026-08-31 — the writes stall the relay loop in busy fights)")
_ap.add_argument("--spfill", action="store_true", help="refill every unit's skill gauges (SP) to 100%% once a second")
_ap.add_argument("--cheat", type=float, default=1.0, help="(ineffective, kept for experiments) scale HeroParam hp/attack")
_ap.add_argument("--log", default=os.path.join(HERE, "photon_lite.log"), help="log file")
_ARGS = _ap.parse_args()

LAN_IP = _ARGS.lan_ip or _detect_lan_ip()
MASTER_PORT = _ARGS.port
GAME_PORT = _ARGS.game_port or MASTER_PORT + 1
LOG = open(_ARGS.log, "a", encoding="utf-8")
T0 = time.time()
LOCK = threading.Lock()
CHEAT = _ARGS.cheat
FILL = _ARGS.fill
GHOST = not _ARGS.no_ghost
GHOST_ACTOR = 2
GODMODE = _ARGS.godmode
GOD_INTERVAL = 0.2
HEAL_HITATTR_INDEX = 226   # BUF_127_HEAL_LV01 -- index into the 2.19.0 client's PlayerActionHitAttribute master list
SHIELD_HITATTR_INDEX = 8252  # S169_001_02_LV02 -> ActionCondition 2065: damage shield 100% max HP (2 charges)
BUFF_PERIOD = 2.0            # how often each unit's shield/cut/attack-buff volley refreshes (staggered, one unit per tick)
BUFF_HITATTR_INDEXES = [SHIELD_HITATTR_INDEX, 439, 438, 5567, 5566]   # 70%/70%/60%/50% damage-cut buffs
# Dragon-gauge refill: S053_001_DPC_LV02 (index 7499) = +20% dragon points to self each application, so a boss that
# drains the gauge (Diabolos 背德祝福) cannot keep the party from shapeshifting. Applied with the buffs every second.
DP_HITATTR_INDEX = 7499
# +100/+50/+50/+15/+10% attack, all _CurseOfEmptinessInvalid. NEVER add rows whose ActionCondition carries
# _EnhancedSkill*/_EnhancedBurstAttack (e.g. condition 853): they replace the units' skills and freeze the game.
ATK_BUFF_HITATTR_INDEXES = [6579, 5408, 73, 8608, 5458] if _ARGS.atkbuff else []
EV_RECOVERY_HP_REQUEST = 76
# --spfill: RecoverySpRequest (74) = [seq, character[actor,idx], healRatio, healSkillIndex, isHumanOnly, healValue,
# isDragonOnly] (RecoverySpRequestFormatter.Serialize @0x24337AC, wire order byte-verified). The owner
# (MultiPlayManager.OnEvent case 74, HasMultiPlayOwner) branches on Mathf.Approximately(0, healRatio): ratio != 0 ->
# virtual RecoverySpRatio (every skill of the current form gains ceil(consumeSp * ratio)); ratio ~0 ->
# CharacterBase.RecoverySp(healValue, i) per skill, clamped to max. BOTH forms are sent back-to-back each tick because
# a phone bisect (2026-08-31, 5 runs) found NEITHER form fills anything alone, yet the pair does — an unexplained
# client-side coupling, reproduced twice. A HEAL_SP hit attribute (280) via RecoveryHpRequest was also tried: inert
# alone and dead weight in the triple, so it was dropped. Details: photon-research README.txt "SPFILL".
SPFILL = _ARGS.spfill
EV_RECOVERY_SP_REQUEST = 74
SP_INTERVAL = 1.0
# Party-switch quests: the second team's units are CharacterId index 40+i (client CharacterId.LatterPartyIndexOffset = 40;
# ServantIndexOffset 20, GuestPlayerIndexOffset 100). Every per-unit cheat targets all parties, or team 2 gets nothing.
LATTER_PARTY_INDEX_OFFSET = 40
EV_REBORN = 27
REBORN_DELAY = 0.4
# --cleanse: the client broadcasts every condition applied to its units as ChangeBuff (50); entries whose `from`
# actor is -1 (an enemy) / hitTargetGroup 3 are debuffs -> answer with ResetBuffRequest (75), which the owner applies
# (MultiPlayManager.OnEvent case 75 @0x1DD2FD8 -> HasMultiPlayOwner -> CharacterBuff.ResetBuffDebuffByConditionId).
CLEANSE = (_ARGS.cleanse or _ARGS.godmode) and not _ARGS.no_cleanse
LOG_BUFFS = _ARGS.log_buffs
EV_CHANGE_BUFF = 50
EV_RESET_BUFF_REQUEST = 75
# Conditions that resist ResetBuffRequest by design (虛無/nihil 1599, 標的/lock-on 1671): resets for them are pure
# event-channel load and never remove anything, so cleanse skips them (2026-08-31 slowdown fix — the Diabolos AoE
# applies 3 conditions per unit at once and only 惡魔審判 1989 is actually removable).
CLEANSE_SKIP_CONDITIONS = {1599, 1671}
# Party-switch quests: at each phase the host raises GameStepEvent (96, [seq, step]) and MultiPlayWaitingList
# .StartWaitForAllOthers waits for the same step from every other actor (PartySwitchTimeout after ~10 s otherwise).
# The ghost echoes each step as its own event (sender = ghost actor).
EV_GAME_STEP = 96


def now_ms():
    return int((time.time() - T0) * 1000) & 0xFFFFFFFF


def log(s):
    line = f"{datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]} {s}"
    with LOCK:
        print(line, flush=True)
        LOG.write(line + "\n")
        LOG.flush()


# ----------------------------------------------------------------------------------------------- Dawnshard / plugin port
API_BASE = f"{_ARGS.api.rstrip('/')}/{_ARGS.api_prefix}"
PHOTON_TOKEN = _ARGS.token
REPLAY_TIMEOUT_SECONDS = 30
EV_READY, EV_CHARACTER_DATA, EV_START_QUEST, EV_ROOM_BROKEN, EV_GAME_SUCCEED = 0x03, 0x14, 0x15, 0x17, 0x18
EV_PARTY, EV_CLEAR_REQ, EV_CLEAR_RESP, EV_FAIL_REQ, EV_FAIL_RESP, EV_DEAD = 0x3E, 0x3F, 0x40, 0x43, 0x44, 0x48
EV_NAMES = {3: "Ready", 0x14: "CharacterData", 0x15: "StartQuest", 0x17: "RoomBroken", 0x18: "GameSucceed", 0x3E: "Party",
            27: "RebornEvent", 76: "RecoveryHpRequest", 61: "DragonGauge", 71: "EnemyAbility", 50: "ChangeBuff", 75: "ResetBuffRequest",
            0x3F: "ClearQuestRequest", 0x40: "ClearQuestResponse", 0x43: "FailQuestRequest", 0x44: "FailQuestResponse",
            0x48: "Dead", 253: "PropertiesChanged", 255: "Join", 254: "Leave"}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def mp_pack(obj):
    return msgpack.packb(obj, use_bin_type=True, use_single_float=True)


def mp_unpack(b):
    """MessagePack, accepting MessagePack-CSharp Lz4Block (ext 99: msgpack int = original size, then an lz4 block)."""
    b = bytes(b)
    if b and b[0] in (0xC7, 0xC8, 0xC9):
        if b[0] == 0xC7: n, t, off = b[1], b[2], 3
        elif b[0] == 0xC8: n, t, off = int.from_bytes(b[1:3], "big"), b[3], 4
        else: n, t, off = int.from_bytes(b[1:5], "big"), b[5], 6
        if t == 99:
            payload = b[off:off + n]
            unp = msgpack.Unpacker(); unp.feed(payload)
            size = unp.unpack(); consumed = unp.tell()
            raw = lz4.block.decompress(payload[consumed:], uncompressed_size=size)
            return msgpack.unpackb(raw, raw=False, strict_map_key=False)
    return msgpack.unpackb(b, raw=False, strict_map_key=False)


def _load_heroparam_keys():
    return [tuple(x) for x in json.load(open(os.path.join(HERE, "heroparam_keys.json"), encoding="utf-8"))["keys"]]


HEROPARAM_KEYS = _load_heroparam_keys()
HEROPARAM_LEN = max(k for k, _, _ in HEROPARAM_KEYS) + 1
_DEFAULTS = {"int": 0, "long": 0, "float": 0.0, "double": 0.0, "bool": False, "int[]": [], "string": ""}


def hero_to_array(h):
    """JSON HeroParam (camelCase, from heroparam/batch) -> MessagePack-CSharp int-key array (nil holes)."""
    arr = [None] * HEROPARAM_LEN
    for k, typ, jname in HEROPARAM_KEYS:
        v = h.get(jname)
        if v is None:
            v = _DEFAULTS.get(typ, 0)
        elif typ == "float" or typ == "double":
            v = float(v)
        if CHEAT != 1.0 and jname in ("hp", "attack"):
            v = int(round(int(v) * CHEAT))
        arr[k] = v
    return arr


def api_post(path, body, headers):
    """POST via curl.exe: Python's http.client hangs on localhost chunked responses on this PC (Norton), curl does not."""
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile(dir=HERE, prefix="req_", suffix=".bin", delete=False) as f:
        f.write(body); req_path = f.name
    out_path = req_path.replace("req_", "resp_")
    cmd = ["curl.exe", "-s", "--noproxy", "*", "--max-time", "20", "-X", "POST", f"{API_BASE}/{path}",
           "--data-binary", f"@{req_path}", "-o", out_path, "-w", "%{http_code}"]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        code = int((r.stdout or "0").strip() or 0)
        data = open(out_path, "rb").read() if os.path.exists(out_path) else b""
    finally:
        for p in (req_path, out_path):
            try: os.remove(p)
            except OSError: pass
    if code >= 400:
        raise urllib.error.HTTPError(f"{API_BASE}/{path}", code, data[:200].decode("utf-8", "replace"), {}, None)
    if code == 0:
        raise RuntimeError(f"curl failed: {r.stderr.strip()[:200]}")
    return code, data


# ----------------------------------------------------------------------------------------------- crypto (client-verified)
OAKLEY_768 = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A63A3620FFFFFFFFFFFFFFFF", 16)
GENERATOR = 22


def be_bytes(n):
    return n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b"\0"


class DH:
    def __init__(self):
        self.secret = random.getrandbits(160) | (1 << 159)
        self.public = pow(GENERATOR, self.secret, OAKLEY_768)
        self.key = None

    def derive(self, other_pub_bytes):
        other = int.from_bytes(other_pub_bytes, "big")
        shared = pow(other, self.secret, OAKLEY_768)
        self.key = hashlib.sha256(be_bytes(shared)).digest()

    def encrypt(self, data):
        pad = 16 - len(data) % 16
        data = data + bytes([pad]) * pad
        enc = Cipher(algorithms.AES(self.key), modes.CBC(b"\0" * 16)).encryptor()
        return enc.update(data) + enc.finalize()

    def decrypt(self, data):
        dec = Cipher(algorithms.AES(self.key), modes.CBC(b"\0" * 16)).decryptor()
        out = dec.update(data) + dec.finalize()
        return out[:-out[-1]] if out and 1 <= out[-1] <= 16 else out


# ----------------------------------------------------------------------------------------------- Protocol16 codec
class Byte(int): pass
class Short(int): pass
class Long(int): pass
class Float(float): pass
class IntArray(list): pass
class StrArray(list): pass
class ObjArray(list): pass
class TypedArray(list):  # Protocol16 'y' array: fixed element type code
    etype = 0x69
class Custom:
    def __init__(self, code, data): self.code, self.data = code, data
    def __repr__(self): return f"Custom({self.code},{self.data.hex()})"


def p16_decode(buf, pos):
    t = buf[pos]; pos += 1
    return _p16_value(buf, pos, t)


def _p16_value(buf, pos, t):
    if t == 0x2A: return None, pos
    if t == 0x62: return Byte(buf[pos]), pos + 1
    if t == 0x6F: return bool(buf[pos]), pos + 1
    if t == 0x6B: return Short(struct.unpack_from(">h", buf, pos)[0]), pos + 2
    if t == 0x69: return struct.unpack_from(">i", buf, pos)[0], pos + 4
    if t == 0x6C: return Long(struct.unpack_from(">q", buf, pos)[0]), pos + 8
    if t == 0x66: return Float(struct.unpack_from(">f", buf, pos)[0]), pos + 4
    if t == 0x64: return struct.unpack_from(">d", buf, pos)[0], pos + 8
    if t == 0x73:
        n = struct.unpack_from(">H", buf, pos)[0]; pos += 2
        return buf[pos:pos + n].decode("utf-8", "replace"), pos + n
    if t == 0x78:
        n = struct.unpack_from(">i", buf, pos)[0]; pos += 4
        return bytes(buf[pos:pos + n]), pos + n
    if t == 0x6E:
        n = struct.unpack_from(">i", buf, pos)[0]; pos += 4
        return IntArray(struct.unpack_from(f">{n}i", buf, pos)), pos + 4 * n
    if t == 0x61:
        n = struct.unpack_from(">H", buf, pos)[0]; pos += 2
        out = StrArray()
        for _ in range(n):
            v, pos = _p16_value(buf, pos, 0x73); out.append(v)
        return out, pos
    if t == 0x68:
        n = struct.unpack_from(">H", buf, pos)[0]; pos += 2
        d = {}
        for _ in range(n):
            k, pos = p16_decode(buf, pos); v, pos = p16_decode(buf, pos); d[k] = v
        return d, pos
    if t == 0x44:
        kt, vt = buf[pos], buf[pos + 1]; pos += 2
        n = struct.unpack_from(">H", buf, pos)[0]; pos += 2
        d = {}
        for _ in range(n):
            if kt == 0: k, pos = p16_decode(buf, pos)
            else: k, pos = _p16_value(buf, pos, kt)
            if vt == 0: v, pos = p16_decode(buf, pos)
            else: v, pos = _p16_value(buf, pos, vt)
            d[k] = v
        return d, pos
    if t == 0x7A:
        n = struct.unpack_from(">H", buf, pos)[0]; pos += 2
        out = ObjArray()
        for _ in range(n):
            v, pos = p16_decode(buf, pos); out.append(v)
        return out, pos
    if t == 0x79:
        n = struct.unpack_from(">H", buf, pos)[0]; pos += 2
        et = buf[pos]; pos += 1
        out = TypedArray(); out.etype = et
        for _ in range(n):
            v, pos = _p16_value(buf, pos, et); out.append(v)
        return out, pos
    if t == 0x63:
        code = buf[pos]; n = struct.unpack_from(">H", buf, pos + 1)[0]; pos += 3
        return Custom(code, bytes(buf[pos:pos + n])), pos + n
    raise ValueError(f"unknown p16 type 0x{t:02x} at {pos - 1}")


def p16_encode(v):
    if v is None: return b"\x2a"
    if isinstance(v, bool): return b"\x6f" + bytes([1 if v else 0])
    if isinstance(v, Byte): return b"\x62" + bytes([v & 0xFF])
    if isinstance(v, Short): return b"\x6b" + struct.pack(">h", v)
    if isinstance(v, Long): return b"\x6c" + struct.pack(">q", v)
    if isinstance(v, int): return b"\x69" + struct.pack(">i", v)
    if isinstance(v, Float): return b"\x66" + struct.pack(">f", v)
    if isinstance(v, float): return b"\x64" + struct.pack(">d", v)
    if isinstance(v, str):
        b = v.encode("utf-8"); return b"\x73" + struct.pack(">H", len(b)) + b
    if isinstance(v, (bytes, bytearray)): return b"\x78" + struct.pack(">i", len(v)) + bytes(v)
    if isinstance(v, IntArray): return b"\x6e" + struct.pack(">i", len(v)) + struct.pack(f">{len(v)}i", *v)
    if isinstance(v, StrArray):
        return b"\x61" + struct.pack(">H", len(v)) + b"".join(p16_encode(s)[1:] for s in v)
    if isinstance(v, ObjArray): return b"\x7a" + struct.pack(">H", len(v)) + b"".join(p16_encode(x) for x in v)
    if isinstance(v, TypedArray):
        return b"\x79" + struct.pack(">H", len(v)) + bytes([v.etype]) + b"".join(p16_encode(x)[1:] for x in v)
    if isinstance(v, list):  # untyped python list: treat as int[] ('y' of 'i') -- what UsePartySlot uses
        return b"\x79" + struct.pack(">H", len(v)) + b"\x69" + b"".join(struct.pack(">i", int(x)) for x in v)
    if isinstance(v, dict):
        return b"\x68" + struct.pack(">H", len(v)) + b"".join(p16_encode(k) + p16_encode(x) for k, x in v.items())
    if isinstance(v, Custom): return b"\x63" + bytes([v.code]) + struct.pack(">H", len(v.data)) + v.data
    raise TypeError(f"cannot encode {type(v)}")


def params_decode(buf, pos):
    n = struct.unpack_from(">H", buf, pos)[0]; pos += 2
    d = {}
    for _ in range(n):
        k = buf[pos]; pos += 1
        v, pos = p16_decode(buf, pos); d[k] = v
    return d, pos


def params_encode(d):
    return struct.pack(">H", len(d)) + b"".join(bytes([k]) + p16_encode(v) for k, v in d.items())


# ----------------------------------------------------------------------------------------------- names for the log
OPS = {230: "Authenticate", 231: "AuthenticateOnce", 229: "JoinLobby", 228: "LeaveLobby", 227: "CreateGame", 226: "JoinGame",
       225: "JoinRandomGame", 254: "Leave", 253: "RaiseEvent", 252: "SetProperties", 251: "GetProperties", 248: "ChangeGroups",
       219: "FindFriends", 218: "GetLobbyStats", 217: "GetRegions", 220: "GetGameList", 222: "Rpc", 0: "InitEncryption(internal)",
       1: "Ping(internal)"}
PARAMS = {230: "Address", 224: "ApplicationId", 221: "Secret", 220: "AppVersion", 225: "UserId", 217: "ClientAuthenticationType",
          216: "ClientAuthenticationParams", 214: "ClientAuthenticationData", 215: "JoinMode", 210: "Region", 213: "LobbyName",
          212: "LobbyType", 211: "LobbyStats", 255: "RoomName", 248: "GameProperties", 249: "PlayerProperties", 250: "Broadcast",
          252: "ActorList", 254: "ActorNr", 251: "Properties", 253: "TargetActorNr", 247: "Cache", 246: "ReceiverGroup", 245: "Data",
          244: "Code", 240: "Group", 239: "Remove", 238: "Add", 236: "EmptyRoomTTL", 235: "PlayerTTL", 234: "EventForward",
          233: "IsInactive", 232: "CheckUserOnJoin", 231: "ExpectedValues", 223: "MatchMakingType", 222: "GameList", 202: "NickName",
          204: "PluginName", 201: "PluginVersion", 195: "ExpectedProtocol", 194: "CustomInitData", 193: "EncryptionMode",
          192: "EncryptionData", 191: "WebFlags", 190: "RoomOptionFlags"}


def fmt_params(d):
    return "{" + ", ".join(f"{k}({PARAMS.get(k, '?')})={v!r}" for k, v in d.items()) + "}"


# ----------------------------------------------------------------------------------------------- ENet transport
CRC_ENABLED = 0xCC
CT_ACK, CT_CONNECT, CT_VERIFY, CT_DISCONNECT, CT_PING, CT_RELIABLE, CT_UNRELIABLE, CT_FRAGMENT = 1, 2, 3, 4, 5, 6, 7, 8
CT_NAMES = {1: "Ack", 2: "Connect", 3: "VerifyConnect", 4: "Disconnect", 5: "Ping", 6: "SendReliable", 7: "SendUnreliable",
            8: "SendFragment", 11: "ServerTime", 12: "SendUnsequenced"}


def photon_crc(buf):
    return (zlib.crc32(buf) ^ 0xFFFFFFFF) & 0xFFFFFFFF


class Peer:
    def __init__(self, server, addr):
        self.server, self.addr = server, addr
        self.peer_id = 1
        self.challenge = 0
        self.out_seq = {}
        self.dh = DH()
        self.encrypted = False
        self.fragments = {}
        self.state = "new"
        self.game_props = {}
        self.actor_props = {}
        self.room_name = None
        # plugin state (GoToIngameStateManager / GameLogicPlugin), single actor nr 1
        self.min_state = 0
        self.hero = {}          # actorNr -> {"lists": [[heroparam json...], ...], "used": n}
        self.ready = set()
        self.dead = set()
        self.start_actor_count = 0
        self.ghost_props = None  # actor props of the seated ghost player (non-raid rooms), None = no ghost
        self.lock = threading.RLock()   # send path is used from the UDP thread and the god-mode thread
        self.in_quest = False
        self.ev_seq = {}                # event code -> running _raiseEventSequenceId
        self.cleanse_recent = {}        # (unit tuple, conditionId) -> time of the last cleanse volley (dedupe)
        # Reliable delivery (2026-09-01): BlueStacks' NAT drops packets when a >~64 KB burst overflows the
        # client's UDP receive buffer (observed: ack gap at seq ~128 of a 202-fragment join+ghost burst), and
        # ENet delivers reliably IN ORDER, so one lost fragment stalls the client's receive stream forever
        # (ghost invisible, raid start aborting with "matching disconnect1"). Fix = real ENet behaviour:
        # cap in-flight unacked packets (window) and retransmit on timeout until acked.
        self.unacked = {}               # (chan, seq) -> [cmd_bytes, next_resend_monotonic_s, resend_count]
        self.sendq = collections.deque()  # (chan, seq, cmd_bytes) built but deferred while the window is full

    def tag(self):
        return f"[{self.server.role}:{self.addr[0]}:{self.addr[1]}]"

    # ---- packet building
    def send_cmds(self, cmds):
        hdr = struct.pack(">HBBII", self.peer_id, CRC_ENABLED, len(cmds), now_ms(), self.challenge) + b"\0\0\0\0"
        pkt = bytearray(hdr + b"".join(cmds))
        struct.pack_into(">I", pkt, 12, photon_crc(bytes(pkt)))
        self.server.sock.sendto(bytes(pkt), self.addr)

    def cmd(self, ctype, chan, flags, payload, relseq):
        return struct.pack(">BBBBII", ctype, chan, flags, 4, 12 + len(payload), relseq) + payload

    def next_seq(self, chan):
        self.out_seq[chan] = self.out_seq.get(chan, 0) + 1
        return self.out_seq[chan]

    def ack(self, chan, relseq, senttime):
        return self.cmd(CT_ACK, chan, 0, struct.pack(">II", relseq, senttime), 0)

    FRAG_SIZE = 480  # client MTU is 576 (Connect payload); 16 hdr + 12 cmd + 20 fragment hdr + 480 = 528 bytes/datagram
    WINDOW = 48       # max unacked reliable packets in flight (~25 KB wire) — stays under the ~64 KB receive
                      # buffer that BlueStacks overflowed at ~128 packets; LAN acks refill it within a few ms
    RTO_S = 0.25      # first retransmission after 250 ms, doubling per resend up to RTO_MAX_S
    RTO_MAX_S = 2.0
    RESEND_GIVEUP = 15  # ~20 s of retries, then the packet is dropped (peer is gone)

    def send_reliable(self, chan, payload, extra_cmds=()):
        with self.lock:
            self._send_reliable(chan, payload, extra_cmds)

    def _queue_reliable(self, ctype, chan, payload):
        seq = self.next_seq(chan)
        self.sendq.append((chan, seq, self.cmd(ctype, chan, 1, payload, seq)))
        return seq

    def _pump_sendq(self):
        t = time.monotonic()
        while self.sendq and len(self.unacked) < self.WINDOW:
            chan, seq, c = self.sendq.popleft()
            self.unacked[(chan, seq)] = [c, t + self.RTO_S, 0]
            self.send_cmds([c])

    def on_ack(self, chan, aseq):
        with self.lock:
            self.unacked.pop((chan, aseq), None)
            self._pump_sendq()

    def retransmit_due(self):
        """Called from the retransmit thread every ~100 ms; resends unacked reliable packets past their RTO."""
        with self.lock:
            if self.state == "disconnected" or not self.unacked:
                return
            t = time.monotonic()
            resent = dropped = 0
            for key, ent in list(self.unacked.items()):
                if t < ent[1]:
                    continue
                if ent[2] >= self.RESEND_GIVEUP:
                    del self.unacked[key]
                    dropped += 1
                    continue
                ent[2] += 1
                ent[1] = t + min(self.RTO_S * (2 ** ent[2]), self.RTO_MAX_S)
                try:
                    self.send_cmds([ent[0]])
                except OSError:
                    pass
                resent += 1
            if resent or dropped:
                log(f"{self.tag()} retransmit: {resent} pkt(s) resent"
                    + (f", {dropped} given up" if dropped else "")
                    + f" ({len(self.unacked)} in flight, {len(self.sendq)} queued)")
            self._pump_sendq()

    def _send_reliable(self, chan, payload, extra_cmds=()):
        if len(payload) <= self.FRAG_SIZE:
            if extra_cmds:
                self.send_cmds(list(extra_cmds))
            self._queue_reliable(CT_RELIABLE, chan, payload)
            self._pump_sendq()
            return
        # Photon fragmentation: each fragment is its own reliable command (own sequence number); payload header =
        # startSequenceNumber, fragmentCount, fragmentNumber, totalLength, fragmentOffset (all big-endian int32).
        total = len(payload)
        count = (total + self.FRAG_SIZE - 1) // self.FRAG_SIZE
        start = self.out_seq.get(chan, 0) + 1
        if extra_cmds:
            self.send_cmds(list(extra_cmds))
        for i in range(count):
            off = i * self.FRAG_SIZE
            chunk = payload[off:off + self.FRAG_SIZE]
            hdr = struct.pack(">IIIII", start, count, i, total, off)
            self._queue_reliable(CT_FRAGMENT, chan, hdr + chunk)
        self._pump_sendq()
        log(f"{self.tag()} <- (sent {total} bytes as {count} fragments, seq {start}..{start + count - 1})")

    # ---- message building
    def send_op_response(self, opcode, params, retcode=0, debug=None, encrypt=False):
        body = bytes([opcode]) + struct.pack(">h", retcode) + p16_encode(debug) + params_encode(params)
        if encrypt and self.dh.key:
            msg = b"\xf3\x83" + self.dh.encrypt(body)
        else:
            msg = b"\xf3\x03" + body
        log(f"{self.tag()} <- OpResponse {opcode}({OPS.get(opcode, '?')}) ret={retcode} {fmt_params(params)}")
        self.send_reliable(0, msg)

    def send_internal_response(self, opcode, params, retcode=0):
        body = bytes([opcode]) + struct.pack(">h", retcode) + b"\x2a" + params_encode(params)
        log(f"{self.tag()} <- InternalOpResponse {opcode} {fmt_params(params)}")
        self.send_reliable(0, b"\xf3\x07" + body)

    def send_event(self, evcode, params):
        body = bytes([evcode]) + params_encode(params)
        if evcode not in (EV_RECOVERY_HP_REQUEST, EV_RECOVERY_SP_REQUEST, EV_RESET_BUFF_REQUEST):   # god-mode/cleanse loops are too chatty to log every send
            log(f"{self.tag()} <- Event {evcode} {fmt_params(params)}")
        self.send_reliable(0, b"\xf3\x04" + body)

    # ---- incoming
    def on_packet(self, data):
        if len(data) < 12:
            log(f"{self.tag()} short packet {data.hex()}"); return
        peer_id, flags, ncmd, senttime, challenge = struct.unpack_from(">HBBII", data, 0)
        off = 12
        if flags == CRC_ENABLED:
            crc = struct.unpack_from(">I", data, 12)[0]
            z = bytearray(data); struct.pack_into(">I", z, 12, 0)
            if photon_crc(bytes(z)) != crc:
                log(f"{self.tag()} BAD CRC, dropped"); return
            off = 16
        self.challenge = challenge
        acks = []
        for i in range(ncmd):
            if off + 12 > len(data):
                log(f"{self.tag()} truncated command {i}"); break
            ctype, chan, cflags, resv, clen, relseq = struct.unpack_from(">BBBBII", data, off)
            payload = data[off + 12:off + clen]
            off += max(clen, 12)
            if cflags & 1 and ctype not in (CT_CONNECT,):
                acks.append(self.ack(chan, relseq, senttime))
            try:
                self.on_command(ctype, chan, cflags, resv, relseq, payload, senttime, peer_id)
            except Exception:
                log(f"{self.tag()} handler error:\n{traceback.format_exc()}")
        if acks:
            self.send_cmds(acks)

    def on_command(self, ctype, chan, cflags, resv, relseq, payload, senttime, hdr_peer):
        name = CT_NAMES.get(ctype, str(ctype))
        if ctype == CT_CONNECT:
            log(f"{self.tag()} -> Connect (hdrPeer={hdr_peer}, challenge={self.challenge}, payload={payload.hex()})")
            if self.state == "new":
                self.state = "connecting"
            verify = bytearray(payload)
            struct.pack_into(">H", verify, 0, self.peer_id)
            with self.lock:
                vseq = self.next_seq(chan)
                vcmd = self.cmd(CT_VERIFY, chan, 1, bytes(verify), vseq)
                self.unacked[(chan, vseq)] = [vcmd, time.monotonic() + self.RTO_S, 0]
                self.send_cmds([self.ack(chan, relseq, senttime), vcmd])
            log(f"{self.tag()} <- Ack + VerifyConnect (peerId={self.peer_id})")
            return
        if ctype == CT_ACK:
            aseq, atime = struct.unpack_from(">II", payload, 0)
            log(f"{self.tag()} -> Ack chan={chan} seq={aseq} (hdrPeer={hdr_peer})")
            self.on_ack(chan, aseq)
            return
        if ctype == CT_DISCONNECT:
            log(f"{self.tag()} -> Disconnect (hdrPeer={hdr_peer}, flags={cflags}, seq={relseq})")
            # The client waits for the server's ack of its Disconnect before it reconnects to the game server.
            self.send_cmds([self.ack(chan, relseq, senttime)])
            log(f"{self.tag()} <- Ack(Disconnect)")
            self.state = "disconnected"
            with self.lock:
                self.unacked.clear()
                self.sendq.clear()
            return
        if ctype == CT_PING:
            log(f"{self.tag()} -> Ping"); return
        if ctype == CT_FRAGMENT:
            start, count, num, total, foff = struct.unpack_from(">IIIII", payload, 0)
            frag = self.fragments.setdefault(start, {"count": count, "total": total, "parts": {}})
            frag["parts"][num] = (foff, payload[20:])
            log(f"{self.tag()} -> Fragment {num + 1}/{count} of {total} bytes (start={start})")
            if len(frag["parts"]) == count:
                buf = bytearray(total)
                for o, p in frag["parts"].values():
                    buf[o:o + len(p)] = p
                del self.fragments[start]
                self.on_message(bytes(buf), chan)
            return
        if ctype in (CT_RELIABLE, CT_UNRELIABLE, 12):
            if ctype == CT_UNRELIABLE:
                if len(payload) <= 4:
                    log(f"{self.tag()} -> {name} chan={chan} flags={cflags} resv={resv} seq={relseq} raw={payload.hex()}")
                    return
                payload = payload[4:]
            self.on_message(payload, chan)
            return
        log(f"{self.tag()} -> {name} chan={chan} flags={cflags} len={len(payload)} {payload.hex()}")

    def on_message(self, msg, chan):
        if len(msg) < 2 or msg[0] not in (0xF3, 0xF4):
            log(f"{self.tag()} -> non-photon message chan={chan} len={len(msg)} {msg.hex()}"); return
        mtype = msg[1] & 0x7F
        encrypted = bool(msg[1] & 0x80)
        body = msg[2:]
        if encrypted:
            if not self.dh.key:
                log(f"{self.tag()} -> encrypted message before key exchange: {msg.hex()}"); return
            body = self.dh.decrypt(body)
            self.encrypted = True
        if mtype == 0:
            self.on_init(body); return
        if mtype in (2, 6):
            opcode = body[0]
            params, _ = params_decode(body, 1)
            kind = "OpRequest" if mtype == 2 else "InternalOpRequest"
            log(f"{self.tag()} -> {kind}{' (enc)' if encrypted else ''} {opcode}({OPS.get(opcode, '?')}) {fmt_params(params)}")
            if mtype == 6:
                self.on_internal_op(opcode, params)
            else:
                self.on_op(opcode, params)
            return
        log(f"{self.tag()} -> message type {mtype}{' (enc)' if encrypted else ''}: {body[:120].hex()}")

    def on_init(self, body):
        # body = [protocol major, minor, sdk id, version bytes..., app id (32 bytes)] -- logged raw + best-effort fields
        app = body[7:39].rstrip(b"\0").decode("ascii", "replace") if len(body) >= 39 else "?"
        log(f"{self.tag()} -> Init raw={body.hex()} protocol={body[0]}.{body[1]} clientBytes={body[2:7].hex()} app={app!r}")
        self.send_reliable(0, b"\xf3\x01")
        log(f"{self.tag()} <- InitResponse")
        self.state = "connected"

    def on_internal_op(self, opcode, params):
        if opcode == 0:  # InitEncryption
            client_pub = params.get(1)
            log(f"{self.tag()} client DH public key: {len(client_pub)} bytes")
            self.dh.derive(client_pub)
            self.send_internal_response(0, {1: be_bytes(self.dh.public)})
            log(f"{self.tag()} shared key derived (sha256 head {self.dh.key[:4].hex()})")
        elif opcode == 1:  # Ping
            self.send_internal_response(1, {1: params.get(1, 0), 2: now_ms() & 0x7FFFFFFF})
        else:
            self.send_internal_response(opcode, {})

    def on_op(self, opcode, params):
        role = self.server.role
        if opcode == 230:  # Authenticate
            resp = {}
            if 225 in params and params[225] is not None:
                resp[225] = params[225]
            if role == "game":
                resp[221] = "photon-lite-secret"
            self.send_op_response(230, resp, encrypt=self.encrypted)
            self.state = "authenticated"
            return
        if opcode == 229:  # JoinLobby
            self.send_op_response(229, {})
            return
        if opcode == 228:
            self.send_op_response(228, {}); return
        if opcode == 225 and role == "master":  # JoinRandomGame: no rooms exist here -> NoRandomMatchFound (32760)
            self.send_op_response(225, {}, retcode=32760, debug="No match found")
            return
        if opcode in (227, 226, 225):  # CreateGame / JoinGame / JoinRandomGame
            self.room_name = params.get(255) or f"room{random.randint(1000, 9999)}"
            if role == "master":
                self.game_props = params.get(248) or {}
                self.actor_props = params.get(249) or {}
                resp = {230: f"{LAN_IP}:{GAME_PORT}", 255: self.room_name, 221: "photon-lite-secret"}
                self.send_op_response(opcode, resp)
                self.server.pending_rooms[self.room_name] = (self.game_props, self.actor_props)
                return
            # game server: build the room
            gp = dict(params.get(248) or {})
            ap = dict(params.get(249) or {})
            if not gp and self.room_name in self.server.pending_rooms:
                gp, ap0 = self.server.pending_rooms[self.room_name]
                ap = ap or ap0
            gp.setdefault(Byte(255), Byte(4))      # MaxPlayers
            gp.setdefault(Byte(254), True)         # IsVisible
            gp.setdefault(Byte(253), True)         # IsOpen
            gp[Byte(248)] = 1                      # MasterClientId
            gp.setdefault("RoomId", random.randint(1_000_000, 9_999_999))
            # Dawnshard's plugin (CollectionExtensions.InitializeViewerId) copies PlayerId -> ViewerId; the client's
            # MatchingService.Update waits for LocalPlayer.CustomProperties["ViewerId"] before State = InRoom.
            if "ViewerId" not in ap and "PlayerId" in ap:
                ap["ViewerId"] = ap["PlayerId"]
            self.game_props, self.actor_props = gp, ap
            resp = {254: 1, 252: IntArray([1]), 248: gp, 249: {1: ap}}
            self.send_op_response(opcode, resp)
            # Photon also sends the Join event (255) to the joining actor itself; MatchingService.OnEvent builds
            # its player list from it (Room.BuildPlayerList).
            self.send_event(255, {254: 1, 252: IntArray([1]), 249: ap})
            self.state = "joined"
            if GHOST and self.quest_id() not in RAID_QUEST_IDS:
                self.seat_ghost()
            return
        if opcode == 252:  # SetProperties
            props = params.get(251) or {}
            target = params.get(254)
            if target is None or target == 0:
                self.game_props.update(props)
            else:
                self.actor_props.update(props)
            self.send_op_response(252, {})
            if params.get(250):
                self.send_event(253, {254: 0 if target is None else target, 251: props})
            if target and "GoToIngameState" in props:
                try:
                    self.on_goto_ingame_state(int(target), int(props["GoToIngameState"]))
                except Exception:
                    log(f"{self.tag()} GoToIngameState handler error:\n{traceback.format_exc()}")
            return
        if opcode == 251:  # GetProperties
            players = {1: self.actor_props}
            if self.ghost_props is not None:
                players[GHOST_ACTOR] = self.ghost_props
            self.send_op_response(251, {248: self.game_props, 249: players})
            return
        if opcode == 253:  # RaiseEvent -- no response unless error; plugin hooks on a few codes
            code = int(params.get(244, -1))
            data = params.get(245)
            if code == EV_CHANGE_BUFF and isinstance(data, (bytes, bytearray)):   # ChangeBuff: buffs/debuffs on a unit
                try:
                    cb = mp_unpack(data)
                    if LOG_BUFFS:
                        log(f"{self.tag()} -> client event 50(ChangeBuff) {cb}")
                    if CLEANSE and self.in_quest:
                        self.cleanse_from_changebuff(cb)
                except Exception:  # noqa: BLE001
                    log(f"{self.tag()} -> client event 50(ChangeBuff) raw={bytes(data).hex()}")
            elif code in (61, 71) and isinstance(data, (bytes, bytearray)):   # DragonGauge / EnemyAbility: decode for diagnosis
                try:
                    log(f"{self.tag()} -> client event {code}({EV_NAMES.get(code, '?')}) {mp_unpack(data)}")
                except Exception:  # noqa: BLE001
                    log(f"{self.tag()} -> client event {code} raw={bytes(data).hex()}")
            elif code not in (12, 13, 17, 31, 65, 86, 99, 111, 10, 57, 85, 49):   # movement/state spam
                log(f"{self.tag()} -> client event {code}({EV_NAMES.get(code, '?')}) data={len(data) if isinstance(data, (bytes, bytearray)) else data}")
            try:
                self.on_client_event(code, data)
            except Exception:
                log(f"{self.tag()} client event handler error:\n{traceback.format_exc()}")
            return
        if opcode == 254:  # Leave
            self.send_op_response(254, {})
            self.state = "left"
            return
        self.send_op_response(opcode, {})


    # ---------------------------------------------------------------- plugin port (GameLogicPlugin / GoToIngameStateManager)
    def viewer_id(self):
        return int(self.actor_props.get("PlayerId", "1"))

    def next_ev_seq(self, code):
        self.ev_seq[code] = (self.ev_seq.get(code, 0) % 65535) + 1
        return self.ev_seq[code]

    def cleanse_from_changebuff(self, cb):
        """ChangeBuff = [seq, character[actor,idx], addParameters[[multiPlayKey, type, conditionId, durationSec,
        durationNum, skillId, actionId, abilityId, productId, rate, hitTargetGroup, from[actor,idx], ...]], ...]"""
        if not isinstance(cb, list) or len(cb) < 3 or not isinstance(cb[1], list):
            return
        character = [int(cb[1][0]), int(cb[1][1])]
        if character[0] != 1:
            return
        found = []
        for p in cb[2] or []:          # addParameters: [key, type, conditionId, dur, num, skill, action, ability, product, rate, group, from, ...]
            if isinstance(p, list) and len(p) >= 12:
                found.append((int(p[2]), int(p[7]), int(p[8]), int(p[10]), p[11]))
        if len(cb) > 8:
            for p in cb[8] or []:      # addUnifiedParameters: [key, conditionId, dur, num, skill, action, ability, product, group, from, ...]
                if isinstance(p, list) and len(p) >= 10:
                    found.append((int(p[1]), int(p[6]), int(p[7]), int(p[8]), p[9]))
        for cond, ability_id, product_id, group, frm in found:
            from_actor = int(frm[0]) if isinstance(frm, list) and frm else 0
            if from_actor == -1 or group == 3:
                if cond in CLEANSE_SKIP_CONDITIONS:
                    continue
                key = (tuple(character), cond)
                if time.time() - self.cleanse_recent.get(key, 0) < 2.5:   # a volley is already in flight
                    continue
                self.cleanse_recent[key] = time.time()
                log(f"{self.tag()} CLEANSE: volley for condition {cond} on unit {character} (sends at 0/0.7/2.0s)")
                # 2026-08-31 惡魔審判 finding (log-proven, 6 waves): one immediate reset strips enemy debuffs from
                # the three AI units but never from the player-controlled lead unit — its removal only succeeds on a
                # DELAYED retry (lead-unit removals landed at +1.2/+2.4/+2.7s = the 0.7s/2.0s repeats). So each
                # detection sends the echoed reset three times: now, +0.7s, +2.0s.
                self.send_cleanse(character, cond, ability_id, product_id)
                threading.Timer(0.7, self.send_cleanse, args=(character, cond, ability_id, product_id)).start()
                threading.Timer(2.0, self.send_cleanse, args=(character, cond, ability_id, product_id)).start()

    def send_cleanse(self, character, cond, ability_id, product_id):
        if not self.in_quest:
            return
        evt = [self.next_ev_seq(EV_RESET_BUFF_REQUEST), character, cond, ability_id, product_id]
        self.send_event(EV_RESET_BUFF_REQUEST, {245: mp_pack(evt), 254: 0})   # quiet: the volley line above logs it

    def send_reborn(self, targets):
        if not self.in_quest:
            return
        evt = [self.next_ev_seq(EV_REBORN), 1, [[int(a), int(i)] for a, i in targets], [1.0] * len(targets), False]
        log(f"{self.tag()} GODMODE: reborn {targets} at 100% HP")
        self.raise_plugin_event(EV_REBORN, evt)

    def god_loop(self):
        """--godmode / --spfill: keep every host unit at full HP (RecoveryHpRequest) and/or full SP (RecoverySpRequest);
        the owning client applies both to the units it owns."""
        time.sleep(4.0)  # let the units spawn
        units = self.used_member_count(1) or 1
        parties = max(1, max((len(h["lists"]) for h in self.hero.values()), default=1))
        targets = [p * LATTER_PARTY_INDEX_OFFSET + i for p in range(parties) for i in range(units)]
        if GODMODE:
            log(f"{self.tag()} GODMODE: healing units {targets} every {GOD_INTERVAL}s (hitattr index {HEAL_HITATTR_INDEX})")
        if SPFILL:
            log(f"{self.tag()} SPFILL: refilling every skill of units {targets} every {SP_INTERVAL}s")
        n = 0
        # 2026-08-31 slowdown fix: the old loop fired every unit's full volley in the same tick — with 8 slots
        # (party-switch quests) that was ~120 reliable events in one burst every second, ~144/s sustained, and the
        # game visibly lagged. Now the work is STAGGERED across ticks and thinned: each unit healed every 0.4s
        # (half the units per 0.2s tick), each unit's buff volley every BUFF_PERIOD 2s (at most one unit's volley
        # per tick), SP refills spread over the second. ~80 events/s, peak burst ~20.
        buff_ticks = max(1, int(round(BUFF_PERIOD / GOD_INTERVAL)))
        sp_every = max(1, int(round(SP_INTERVAL / GOD_INTERVAL)))
        nt = len(targets)
        while self.in_quest and self.state in ("joined",) and n < 7200:
            for k, i in enumerate(targets):
                try:
                    if GODMODE:
                        # RecoveryHpRequest: [seq, character[actorId,index], from[actorId,index], healValue, characterType,
                        #                     elementIndex, actionId, productId, bulletId, skillId, followerAvoid]
                        if (n + k) % 2 == 0:   # each unit every 0.4s; the shield + damage cuts cover the gap
                            evt = [self.next_ev_seq(EV_RECOVERY_HP_REQUEST), [1, i], [1, i], 999999, 0, HEAL_HITATTR_INDEX, 0, 0, 0, 0, 0]
                            self.send_event(EV_RECOVERY_HP_REQUEST, {245: mp_pack(evt), 254: 0})
                        if n % buff_ticks == (k * buff_ticks) // nt:   # shield + damage-cut + attack buffs, one unit's volley per tick
                            for idx in BUFF_HITATTR_INDEXES + ATK_BUFF_HITATTR_INDEXES + [DP_HITATTR_INDEX]:
                                evt = [self.next_ev_seq(EV_RECOVERY_HP_REQUEST), [1, i], [1, i], 1, 0, idx, 0, 0, 0, 0, 0]
                                self.send_event(EV_RECOVERY_HP_REQUEST, {245: mp_pack(evt), 254: 0})
                    if SPFILL and n % sp_every == k % sp_every:   # each unit once per SP_INTERVAL, spread over the ticks
                        # RecoverySpRequest: [seq, character, healRatio, healSkillIndex (0 = all), isHumanOnly, healValue, isDragonOnly]
                        # both forms are required — neither fills alone (see the SPFILL constants block above)
                        evt = [self.next_ev_seq(EV_RECOVERY_SP_REQUEST), [1, i], 1.0, 0, False, 0, False]
                        self.send_event(EV_RECOVERY_SP_REQUEST, {245: mp_pack(evt), 254: 0})
                        evt = [self.next_ev_seq(EV_RECOVERY_SP_REQUEST), [1, i], 0.0, 0, False, 99999, False]
                        self.send_event(EV_RECOVERY_SP_REQUEST, {245: mp_pack(evt), 254: 0})
                except Exception:  # noqa: BLE001
                    log(f"{self.tag()} godmode send error:\n{traceback.format_exc()}"); return
            n += 1
            time.sleep(GOD_INTERVAL)
        log(f"{self.tag()} GODMODE/SPFILL loop ended (in_quest={self.in_quest}, state={self.state}, ticks={n})")

    def quest_id(self):
        return int(self.game_props.get("C0", 0))

    def raise_plugin_event(self, code, obj, target=None):
        payload = mp_pack(obj)
        log(f"{self.tag()} <- plugin event 0x{code:02x}({EV_NAMES.get(code, '?')}) {len(payload)} bytes" + (f" -> actor {target}" if target else ""))
        self.send_event(code, {245: payload, 254: 0})

    def set_game_props(self, props):
        for k, v in props.items():
            if v is None:
                self.game_props.pop(k, None)
            else:
                self.game_props[k] = v
        self.send_event(253, {254: 0, 251: props})

    def set_actor_prop(self, actor, props):
        if actor == GHOST_ACTOR and self.ghost_props is not None:
            self.ghost_props.update(props)
        else:
            self.actor_props.update(props)
        self.send_event(253, {254: actor, 251: props})

    def actor_list(self):
        return IntArray([1, GHOST_ACTOR] if self.ghost_props is not None else [1])

    def seat_ghost(self):
        """Add a second, unit-less actor so QuestButtonEnabledCheck's PlayerCount > 1 passes for a lone host."""
        gp = {}
        for k, v in self.actor_props.items():
            if isinstance(k, str):
                gp[k] = v
        gp["PlayerId"] = str(GHOST_ACTOR)
        gp["ViewerId"] = str(GHOST_ACTOR)
        gp["PlayerName"] = "Ghost"
        gp["ReadyToGo"] = 1
        gp["GoToIngameState"] = 0
        rpd = self.actor_props.get("RoomPlayerData")
        if isinstance(rpd, (bytes, bytearray)):
            try:  # same party card as the host, renamed
                d = mp_unpack(rpd)
                if isinstance(d, dict):
                    d["playerName"] = "Ghost"
                    if "viewerId" in d: d["viewerId"] = GHOST_ACTOR
                    gp["RoomPlayerData"] = msgpack.packb(d, use_bin_type=True)
            except Exception:  # noqa: BLE001
                gp["RoomPlayerData"] = bytes(rpd)
        self.ghost_props = gp
        log(f"{self.tag()} seating ghost actor {GHOST_ACTOR} (non-raid room, quest {self.quest_id()})")
        self.send_event(255, {254: GHOST_ACTOR, 252: self.actor_list(), 249: gp})

    def reset_state_machine(self):
        self.min_state = 0
        self.hero = {}

    def on_goto_ingame_state(self, actor, value):
        # single real actor: the minimum over actors is the value itself; the ghost mirrors the host
        min_value = value
        log(f"{self.tag()} GoToIngameState {value} from actor {actor} (min so far {self.min_state})")
        if self.ghost_props is not None and actor == 1:
            self.set_actor_prop(GHOST_ACTOR, {"GoToIngameState": value})
        if min_value > self.min_state:
            self.min_state = min_value
            self.on_min_state_change()
        elif value == 1 and actor == 1:
            self.min_state = 1
            self.on_min_state_change()
        elif value == 0 and self.game_props.get("IsSoloPlayWithPhoton") is True:
            self.set_goto_ingame_info()
            self.raise_plugin_event(EV_START_QUEST, {})

    def on_min_state_change(self):
        s = self.min_state
        log(f"{self.tag()} min GoToIngameState -> {s}")
        if s == 1:
            self.set_goto_ingame_info()
        elif s == 2:
            self.request_hero_param()
        elif s == 3:
            self.raise_party_event()
        elif s == 4:
            self.raise_character_data()

    def set_goto_ingame_info(self):
        elements = [[1, self.viewer_id()]]         # GoToIngameState { Elements: [ActorData{ActorId, ViewerId}], BrInitData: null }
        if self.ghost_props is not None:
            elements.append([GHOST_ACTOR, GHOST_ACTOR])
        self.set_game_props({"GoToIngameInfo": mp_pack([elements, None])})

    def request_hero_param(self):
        slots = [int(x) for x in self.actor_props.get("UsePartySlot", [1])]
        body = json.dumps([{"questId": self.quest_id(), "actorNr": 1, "viewerId": self.viewer_id(), "partySlots": slots}]).encode()
        status, resp = api_post("heroparam/batch", body, {"Content-Type": "application/json", "Accept": "application/json"})
        data = json.loads(resp.decode("utf-8"))
        for d in data:
            self.hero[int(d["actorNr"])] = {"lists": d["heroParamLists"], "used": 0}
        counts = {a: [len(l) for l in h["lists"]] for a, h in self.hero.items()}
        log(f"{self.tag()} heroparam/batch {status}: units per party {counts}")

    def used_member_count(self, actor):
        return self.hero.get(actor, {}).get("used", 0)

    def raise_party_event(self):
        # raid or solo-with-photon -> every actor uses all its units; otherwise MemberCountHelper (1 player -> 3 units)
        is_raid = self.quest_id() in RAID_QUEST_IDS
        table = {}
        for a, h in self.hero.items():
            n = len(h["lists"][0]) if h["lists"] else 0
            table[a] = n if (is_raid or self.game_props.get("IsSoloPlayWithPhoton") is True) else min(FILL, n)
            h["used"] = table[a]
        if self.ghost_props is not None:
            table[GHOST_ACTOR] = 0
        ranking_type = 0
        evt = [1, table, ranking_type, REPLAY_TIMEOUT_SECONDS, False, 0.0, False]
        log(f"{self.tag()} Party: memberCountTable={table} raid={is_raid}")
        self.raise_plugin_event(EV_PARTY, evt)

    def raise_character_data(self):
        for a, h in self.hero.items():
            for heroes in h["lists"]:
                exs = [[int(x.get("position", i)), int(x.get("exAbilityLv", 0)), 0] for i, x in enumerate(heroes)]
                params = [hero_to_array(x) for x in heroes[:h["used"]]]
                if CHEAT != 1.0:
                    log(f"{self.tag()} CHEAT x{CHEAT:g}: hp/attack " +
                        ", ".join(f"{x['characterId']}: {x['hp']}/{x['attack']} -> {p[2]}/{p[3]}" for x, p in zip(heroes[:h['used']], params)))
                evt = [1, a, params, [], exs]
                self.raise_plugin_event(EV_CHARACTER_DATA, evt)
        if self.ghost_props is not None:
            # party-switch quests (_DungeonType 15, e.g. Diabolos/天魔) need one CharacterData per party for EVERY
            # actor: CharacterManager.LoadPlayers indexes otherCharacters[actor][partySwitchIndex] — one empty entry
            # for a 2-party quest hangs the loading coroutine. Mirror the host's party count.
            parties = max(1, max((len(h["lists"]) for h in self.hero.values()), default=1))
            for _ in range(parties):
                self.raise_plugin_event(EV_CHARACTER_DATA, [1, GHOST_ACTOR, [], [], []])

    def on_client_event(self, code, data):
        if code == EV_READY:
            self.ready.add(1)
            log(f"{self.tag()} actor 1 ready -> StartQuest")
            self.raise_plugin_event(EV_START_QUEST, {})
            self.start_actor_count = 1
            if (GODMODE or SPFILL) and not self.in_quest:      # the client sends Ready more than once; one loop per quest
                self.in_quest = True
                threading.Thread(target=self.god_loop, daemon=True).start()
            self.in_quest = True
        elif code == EV_CLEAR_REQ:
            self.in_quest = False
            req = mp_unpack(data)               # [seq, RecordMultiRequest bytes]
            body = mp_unpack(req[1])
            # Omit connecting_viewer_id_list entirely: if the key is present (even []), Dawnshard's record_multi runs
            # ProcessFirstMeetingRewards = its stub co-op social reward (100 free Diamantium per OTHER player met),
            # which for a solo room shows a "Diamantium x0" line on the results screen and mails a x0 present every
            # clear. With the key missing the server takes the GetTeammates fallback (empty -> no bonus, no present).
            body.pop("connecting_viewer_id_list", None)
            body["is_host"] = True
            body["member_count"] = self.used_member_count(1)
            raw = msgpack.packb(body, use_bin_type=True)
            headers = {"Content-Type": "application/octet-stream", "Accept": "application/octet-stream",
                       "Auth-ViewerId": str(self.viewer_id()), "Authorization": f"Bearer {PHOTON_TOKEN}",
                       "RoomName": self.room_name or "", "RoomId": str(self.game_props.get("RoomId", 0))}
            log(f"{self.tag()} ClearQuestRequest: keys={list(body.keys())} -> record_multi")
            try:  # evidence for stat tuning: the client's own damage/HP figures
                pr = body.get("play_record") or {}
                summary = {k: v for k, v in pr.items() if isinstance(v, (int, float, list, dict)) and k in
                           ("time", "total_play_damage", "damage_record", "dragon_damage_record", "is_clear", "wave",
                            "live_unit_no_list", "max_damage", "chara_hp", "hp_record", "damage_hp")}
                log(f"{self.tag()} play_record: {json.dumps(summary, default=str)[:600]}")
            except Exception:  # noqa: BLE001
                pass
            try:
                status, resp = api_post("dungeon_record/record_multi", raw, headers)
            except urllib.error.HTTPError as e:
                log(f"{self.tag()} record_multi HTTP {e.code}: {e.read()[:200]!r}")
                return
            log(f"{self.tag()} record_multi {status}: {len(resp)} bytes")
            room_id = int(self.game_props.get("RoomId", 0))
            if room_id > 0:
                self.set_game_props({"GoToIngameInfo": None, "RoomId": -random.randint(1_000_000, 9_999_999)})
            self.ready.discard(1); self.dead.discard(1)
            self.raise_plugin_event(EV_CLEAR_RESP, [1, resp, 0, False], target=1)
        elif code == EV_GAME_SUCCEED:
            self.reset_state_machine()
            self.ready.clear(); self.dead.clear()
            self.raise_plugin_event(EV_GAME_SUCCEED, {})
            self.set_game_props({"RoomId": random.randint(1_000_000, 9_999_999)})
        elif code == EV_FAIL_REQ:
            self.in_quest = False
            req = mp_unpack(data)
            fail_type = int(req[1]) if isinstance(req, list) and len(req) > 1 else 0
            self.ready.discard(1)
            self.set_actor_prop(1, {"GoToIngameState": 0})
            self.raise_plugin_event(EV_FAIL_RESP, [1, 0 if fail_type == 0 else 1], target=1)
            # lone player: "all dead" or fewer actors than at start -> back to lobby
            self.set_game_props({"GoToIngameInfo": None, "RoomId": -1})
            self.reset_state_machine()
        elif code == EV_GAME_STEP:
            if self.ghost_props is not None:
                try:
                    step = mp_unpack(data)       # [seq, step]
                    log(f"{self.tag()} GameStepEvent from host {step} -> ghost echoes it")
                    evt = [self.next_ev_seq(EV_GAME_STEP)] + (list(step[1:]) if isinstance(step, list) else [step])
                    self.send_event(EV_GAME_STEP, {245: mp_pack(evt), 254: GHOST_ACTOR})
                except Exception:  # noqa: BLE001
                    log(f"{self.tag()} GameStepEvent echo error:\n{traceback.format_exc()}")
        elif code == EV_DEAD:
            self.dead.add(1)
            if GODMODE and self.in_quest:
                try:
                    d = mp_unpack(data)          # Dead: [seq, character[actorId, index], popCount]
                    target = d[1] if isinstance(d, list) and len(d) > 1 else None
                except Exception:  # noqa: BLE001
                    target = None
                if isinstance(target, list) and len(target) >= 2 and int(target[0]) == 1:   # own units only, not enemies
                    threading.Timer(REBORN_DELAY, self.send_reborn, args=([list(target[:2])],)).start()
        elif code == EV_REBORN and GODMODE and self.in_quest:
            try:
                d = mp_unpack(data)              # [seq, type, targetCharas, ratios, isAbilityReborn]
                if isinstance(d, list) and len(d) > 2 and int(d[1]) == 0:   # Wait (downed, waiting for a teammate)
                    targets = [list(t[:2]) for t in d[2] if isinstance(t, list) and len(t) >= 2]
                    if targets:
                        threading.Timer(REBORN_DELAY, self.send_reborn, args=(targets,)).start()
            except Exception:  # noqa: BLE001
                log(f"{self.tag()} reborn parse error:\n{traceback.format_exc()}")


RAID_QUEST_IDS = set(json.load(open(os.path.join(HERE, "raid_quest_ids.json"), encoding="utf-8"))["raid_quest_ids"])


# ----------------------------------------------------------------------------------------------- State Manager HTTP API
# Dawnshard's PhotonStateApi (Features/CoOp/PhotonStateApi.cs) calls GET Get/GameList[?questId], Get/ById/{roomId},
# Get/ByViewerId/{viewerId}, Get/IsHost/{viewerId}; served here from the game server's live rooms.
STATE_PORT = _ARGS.state_port or MASTER_PORT + 2
GAME_SERVER = None


def entry_conditions(blob):
    try:
        v = mp_unpack(blob)   # RoomEntryCondition: [unacceptedElementals, unacceptedWeapons, requiredPower, [objectiveTextId]]
        return {"unacceptedElementTypeList": list(v[0] or []), "unacceptedWeaponTypeList": list(v[1] or []),
                "requiredPartyPower": int(v[2] or 0), "objectiveTextId": int((v[3] or [0])[0])}
    except Exception:  # noqa: BLE001
        return {"unacceptedElementTypeList": [], "unacceptedWeaponTypeList": [], "requiredPartyPower": 0, "objectiveTextId": 0}


def api_game(peer):
    gp, ap = peer.game_props, peer.actor_props
    viewer = int(ap.get("PlayerId", "0") or 0)
    players = [{"actorNr": 1, "viewerId": viewer, "partyNoList": [int(x) for x in ap.get("UsePartySlot", [])]}]
    return {"roomId": int(gp.get("RoomId", 0)), "name": peer.room_name or "", "matchingCompatibleId": int(gp.get("MatchingCompatibleId", 0)),
            "matchingType": int(gp.get("MatchingType", 1)), "questId": int(gp.get("C0", 0)),
            "entryConditions": entry_conditions(gp.get("RoomEntryCondition", b"")),
            "startEntryTime": datetime.datetime.now(datetime.timezone.utc).isoformat(), "players": players,
            "hostViewerId": viewer, "hostPartyNo": players[0]["partyNoList"][0] if players[0]["partyNoList"] else 0,
            "memberNum": len(players), "region": "jp", "clusterName": "jp", "language": str(gp.get("Language", "en_us"))}


def live_rooms():
    if GAME_SERVER is None:
        return []
    return [p for p in list(GAME_SERVER.peers.values()) if p.state == "joined"]


class StateHandler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # route to our log
        log(f"[state] {self.address_string()} {fmt % args}")

    def do_GET(self):
        u = urlparse(self.path)
        parts = [x for x in u.path.split("/") if x]
        rooms = live_rooms()
        if len(parts) >= 2 and parts[0].lower() == "get":
            what = parts[1].lower()
            if what == "gamelist":
                q = parse_qs(u.query).get("questId")
                sel = [p for p in rooms if int(p.game_props.get("MatchingType", 1)) == 1 and p.game_props.get(Byte(254), True)]
                if q:
                    sel = [p for p in sel if int(p.game_props.get("C0", 0)) == int(q[0])]
                return self._json(200, [api_game(p) for p in sel])
            if what == "byid" and len(parts) > 2:
                for p in rooms:
                    if int(p.game_props.get("RoomId", 0)) == int(parts[2]):
                        return self._json(200, api_game(p))
                return self._json(404, {})
            if what == "byviewerid" and len(parts) > 2:
                for p in rooms:
                    if str(p.actor_props.get("PlayerId", "")) == parts[2]:
                        return self._json(200, api_game(p))
                return self._json(404, {})
            if what == "ishost" and len(parts) > 2:
                host = any(str(p.actor_props.get("PlayerId", "")) == parts[2] for p in rooms)
                return self._json(200, host)
        if parts and parts[0].lower() == "ping":
            return self._json(200, "pong")
        return self._json(404, {})

    def do_POST(self):  # Event/* (GameCreate, GameJoin, ...) are what the real plugin would call; nothing to do here
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        return self._json(200, {})


def run_state_http():
    srv = ThreadingHTTPServer(("0.0.0.0", STATE_PORT), StateHandler)
    log(f"[state] State Manager API on http://0.0.0.0:{STATE_PORT}")
    srv.serve_forever()


class Server:
    def __init__(self, role, port):
        self.role, self.port = role, port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", port))
        if hasattr(socket, "SIO_UDP_CONNRESET"):  # Windows: ICMP port-unreachable must not kill recvfrom (WinError 10054)
            self.sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        self.peers = {}
        self.pending_rooms = {}

    def run(self):
        log(f"[{self.role}] listening on UDP 0.0.0.0:{self.port}")
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
            except (ConnectionResetError, OSError) as e:  # Windows reports ICMP port-unreachable here (WinError 10054)
                log(f"[{self.role}] recvfrom: {e!r} (ignored)")
                continue
            peer = self.peers.get(addr)
            if peer is None:
                peer = self.peers[addr] = Peer(self, addr)
                log(f"{peer.tag()} new peer")
            try:
                peer.on_packet(data)
            except Exception:
                log(f"{peer.tag()} packet error:\n{traceback.format_exc()}")


def retransmit_loop(servers):
    while True:
        time.sleep(0.1)
        for srv in servers:
            for peer in list(srv.peers.values()):
                try:
                    peer.retransmit_due()
                except Exception:
                    log(f"{peer.tag()} retransmit error:\n{traceback.format_exc()}")


if __name__ == "__main__":
    master = Server("master", MASTER_PORT)
    game = Server("game", GAME_PORT)
    if CHEAT != 1.0:
        log(f"CHEAT MODE: co-op units get x{CHEAT:g} base hp/attack")
    game.pending_rooms = master.pending_rooms
    GAME_SERVER = game
    threading.Thread(target=game.run, daemon=True).start()
    threading.Thread(target=run_state_http, daemon=True).start()
    threading.Thread(target=retransmit_loop, args=([master, game],), daemon=True).start()
    master.run()
