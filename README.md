# photon-lite

A minimal, free replacement for the Photon multiplayer server that a self-hosted
[Dawnshard](https://github.com/SapiensAnatis/Dawnshard) (Dragalia Lost private server) needs for
co-op. One Python process, no licence, no proprietary SDK — enough for **one player per room**:
every 團隊 / co-op cell in the game (raids and normal co-op) opens its room, starts, is fought with
your full party and clears with rewards, instead of ending in 網路連線中斷 / "network error".

It was built by reading the 2.19.0 iOS client binary and Dawnshard's open-source Photon plugin, and
verified on a real phone. Rooms with a second device are **not** implemented (see *Limits*).

## What it does

* Speaks the client's Photon transport: ENet-style reliable UDP with CRC-32 headers, acks, fragments,
  the `Init` handshake, Photon's Diffie-Hellman/AES message encryption and the Protocol16 serializer.
* Implements the LoadBalancing operations the client uses: Authenticate, JoinLobby, JoinRandomGame,
  CreateGame/JoinGame (master → game-server redirect), SetProperties, RaiseEvent, Leave.
* Ports the room logic of Dawnshard's Photon plugin: `GoToIngameState` 1→4 (`GoToIngameInfo`,
  `heroparam/batch`, `Party`, `CharacterData`), `Ready` → `StartQuest`, `ClearQuestRequest` →
  `dungeon_record/record_multi` → `ClearQuestResponse`, fail/retry/succeed handling.
* Serves the Photon State Manager HTTP API that the Dawnshard container queries
  (`Get/GameList`, `Get/ById`, `Get/ByViewerId`, `Get/IsHost`).
* Lets a lone player start **normal** co-op: the client only enables a normal room's start button with
  more than one player, so the server seats a "ghost" second player (ready, no units), and sends the
  host's whole party (`--fill`, default 4; Dawnshard's rule would be leader + 2 AI).
* Optional cheats (server-injected heal/buff events): `--godmode` (full heal 5×/s, a damage shield and
  70/60/50 % damage-cut buffs) and `--atkbuff` (+225 % attack, immune to Curse of Emptiness).

## Requirements

* Python 3.10+ with `cryptography`, `msgpack`, `lz4` (`pip install cryptography msgpack lz4`).
* `curl` on the PATH (used for the HTTP calls to Dawnshard).
* A running Dawnshard stack reachable from this machine (default `http://127.0.0.1:5000`) whose
  container can reach this machine (`host.docker.internal` on Docker Desktop).
* The phone must reach this machine's UDP ports (default 5055/5056) — same Wi-Fi, no VPN on the phone,
  firewall allowing Python.
* Dragalia Lost client **2.19.0** (the hit-attribute indices used by the cheats are for its master data).

## Setup

1. In Dawnshard's `docker-compose.yml` (environment of the API container):

   ```yaml
   - PhotonOptions__ServerUrl=192.168.1.10:5055          # this PC's LAN IP, master port
   - PhotonOptions__Token=photon-lite-token              # any secret, same as --token
   - PhotonOptions__StateManagerUrl=http://host.docker.internal:5057
   ```

   then `docker compose up -d`. `/load/index` now tells the client to connect to this PC.

2. Run the server:

   ```
   python photon_lite.py --lan-ip 192.168.1.10
   python photon_lite.py --lan-ip 192.168.1.10 --godmode --atkbuff     # with the cheats
   ```

   `--lan-ip` is what the game-server redirect hands to the phone; omit it to auto-detect.
   Log goes to stdout and `photon_lite.log`.

3. On the phone: open any 團隊 cell → **建立房間 / create a room** (auto-join with no room open just
   ends, as in the real game) → start.

## Options

| option | default | meaning |
|---|---|---|
| `--lan-ip` | auto | address the phone connects to for the game server |
| `--port` / `--game-port` / `--state-port` | 5055 / 5056 / 5057 | master UDP, game UDP, State-Manager HTTP |
| `--api`, `--api-prefix` | `http://127.0.0.1:5000`, `2.19.0_20220714193707` | Dawnshard base URL and route prefix |
| `--token` | `photon-lite-token` | must equal `PhotonOptions__Token` |
| `--fill N` | 4 | units a lone player controls in a normal co-op room |
| `--no-ghost` | off | don't seat the ghost second player (normal rooms then can't be started alone) |
| `--godmode` | off | heal every unit 5×/s + 100 %-of-max-HP damage shield + damage-cut buffs every second |
| `--atkbuff` | off | five plain attack-up buffs (+225 % if they stack), refreshed every second |

## Limits

* **One real player per room.** A second device would need JoinGame with a second actor, event relay
  between actors and host-leave handling — not implemented.
* `--godmode` refills HP, it does not make units invulnerable: a hit larger than max HP or a rare
  multi-hit chain can still kill. Auto-revive only works in quests whose `_RebornLimit` allows revives
  (normal co-op quests do; most raids don't).
* Scaling `hp`/`attack` in the HeroParam data does nothing — the client derives unit stats from master
  data + level (`--cheat` is kept only for experiments).
* No random matching with strangers, no room lists across servers.

## How it was made / credits

* Room logic and the HeroParam / State-Manager contracts follow
  [Dawnshard](https://github.com/SapiensAnatis/Dawnshard) (MIT) — `PhotonPlugin/` and
  `PhotonStateManager/`. `heroparam_keys.json` and `raid_quest_ids.json` are derived from its sources.
* Transport, encryption (OakleyPrime768, generator 22, SHA-256 → AES-256-CBC), Protocol16 and the
  client-side expectations (`ViewerId` actor property, `PlayerCount > 1` start rule, hit-attribute
  lookup by index, …) were read from the 2.19.0 client with Il2CppDumper + a disassembler and confirmed
  on the wire. The comments in `photon_lite.py` carry the addresses.
* Not affiliated with or endorsed by Exit Games/Photon, Cygames or Nintendo. Use with your own
  private server only.

## License

MIT — see `LICENSE`.
