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
  a send window with timed retransmission of unacknowledged packets, the `Init` handshake, Photon's
  Diffie-Hellman/AES message encryption and the Protocol16 serializer. The retransmission layer is what
  makes **emulator clients (e.g. an Android client in BlueStacks)** work — their NAT drops packets when
  the multi-fragment join bursts overflow the client's receive buffer, and without resends one lost
  fragment stalls the room forever.
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
* Optional cheats (server-injected events the owning client applies to its own units): `--godmode`
  (full heal 5×/s, a damage shield, 70/60/50 % damage-cut buffs and a debuff cleanse), `--atkbuff`
  (+225 % attack, immune to Curse of Emptiness), `--spfill` (every skill gauge refilled once a
  second, so skills are always ready). The cleanse is closed-loop: each enemy debuff is tracked by its
  sync key and the removal request is repeated every 1.5 s until the client broadcasts that the buff is
  gone (30 s cap). See *Limits* for the one debuff it cannot remove.
* Party-switch quests (two teams, e.g. Diabolos) work: the ghost player follows the host through the
  team-change phases.

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
   python photon_lite.py --lan-ip 192.168.1.10 --godmode --atkbuff --spfill --dragonfill   # with the cheats
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
| `--godmode` | off | heal every unit 2.5×/s + 100 %-of-max-HP damage shield + damage-cut buffs every 2 s (staggered across ticks to keep the event load smooth) |
| `--atkbuff` | off | five plain attack-up buffs (+225 % if they stack), refreshed with the god-mode volley |
| `--cleanse` | off (on with `--godmode`) | remove conditions an enemy puts on your units — closed loop: each application is tracked by its sync key and the removal is retried every 1.5 s until the client confirms it, up to 30 s (see *Limits*) |
| `--no-cleanse` | off | disable the cleanse even with `--godmode` (A/B testing) |
| `--spfill` | off | refill every unit's skill gauges (SP) to 100 % once a second |
| `--dragonfill` | off | **inert, kept for reference** — re-fires QUEST_START DpCharge, which the client only banks at quest start (see *Limits*) |
| `--log-buffs` | off | decode-log every ChangeBuff event (multi-KB lines — heavy in busy fights) |

## Limits

* **One real player per room.** A second device would need JoinGame with a second actor, event relay
  between actors and host-leave handling — not implemented.
* `--godmode` refills HP, it does not make units invulnerable: a hit larger than max HP or a rare
  multi-hit chain can still kill. Auto-revive only works in quests whose `_RebornLimit` allows revives
  (normal co-op quests do; most raids don't).
* **The dragon gauge cannot be refilled from the server.** There is no DP request event; heals
  (`RecoveryHpRequest`) never touch the gauge; `DragonGauge` (61) only updates other players' UI; and
  re-sending `TriggerAbility` QUEST_START does nothing after the fight starts, because the client only
  *banks* quest-start DpCharge values (`GameUserData.questStartChargeRate`) and cashes the bank once in
  `ApplyQuestStartChargeRate`. Verified 2026-09-02 on the wire (gauge 0 after every shapeshift while the
  loop ran all fight) and in the client binary. `--dragonfill` is therefore inert and out of the launcher;
  a real refill would need a client patch (`ConsumeDp` / `SetDp`) or a master-data edit.
* **`--cleanse` cannot clean the avatar you are playing in judgment-mechanic fights.** Established
  across many instrumented runs (Yaldabaoth's party-switch fight, Demonic Judgment): each time the
  boss applies the debuff, the three units you are *not* controlling are cleansed within half a
  second, but the copy on the avatar you are actively using refuses the removal event — 30-second
  barrages and every identifier combination were tried, and the client discards them all. That stack
  is then permanent for the run (later removals only match newer applications), so the avatar you
  play accumulates judgment while the rest of the party stays clean. Switching avatars *after* being
  hit does not free it; a unit hit while you are *not* controlling it cleanses normally. The block is
  inside the client's handling of the controlled character and is not reachable through the co-op
  protocol. (One recorded exception — an AI unit refusing once — remains unexplained.)
* `--cleanse` also skips conditions the game data marks reset-proof (`_ResistDebuffReset`, e.g. the
  Lock-On marker) and Curse of Emptiness.
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
