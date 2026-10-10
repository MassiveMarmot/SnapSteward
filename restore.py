# SPDX-License-Identifier: GPL-3.0-or-later
"""Restore: diff a snap's current connections against its baseline entry
and produce ordered steps. Pure logic; the window runs them through the
normal change flow."""

import interfaces

STEP_FIELDS = ("action", "plug_snap", "plug", "slot_snap", "slot", "tier")


def _current_established(connections, snap_name):
    """{(plug, slot_snap, slot): entry} for snap's established plugs."""
    current = {}
    for c in connections.get("established") or []:
        if not isinstance(c, dict) or not isinstance(c.get("plug"), dict):
            continue
        if c["plug"].get("snap") != snap_name:
            continue
        if not isinstance(c["slot"], dict):
            continue
        key = (str(c["plug"].get("plug") or "?"),
               str(c["slot"].get("snap") or "?"),
               str(c["slot"].get("slot") or "?"))
        current[key] = c
    return current


def _interface_of(connections, snap_name, plug):
    for p in connections.get("plugs") or []:
        if isinstance(p, dict) and p.get("snap") == snap_name \
                and p.get("plug") == plug:
            return p.get("interface")
    return None


def _slot_exists(connections, slot_snap, slot):
    for s in connections.get("slots") or []:
        if isinstance(s, dict) and s.get("snap") == slot_snap \
                and s.get("slot") == slot:
            return True
    return False


def compute_diff(baseline_entry, current_connections, snap_name):
    """Ordered steps, skipped items with reasons, and not-restored
    tier-3 items. Baseline entries name plugs and slots; anything that
    does not exist in the current response is skipped, never sent."""
    steps, skipped, not_restored = [], [], []
    current = _current_established(current_connections, snap_name)
    by_plug = {key[0]: key for key in current}
    for entry in (baseline_entry or {}).get("connected") or []:
        if not isinstance(entry, dict):
            continue
        plug = entry.get("plug")
        slot_snap, slot = entry.get("slot_snap"), entry.get("slot")
        if not all(isinstance(x, str) for x in (plug, slot_snap, slot)):
            skipped.append({"snap": snap_name, "plug": plug,
                           "reason": "invalid baseline entry"})
            continue
        if plug not in by_plug:
            interface = _interface_of(current_connections, snap_name, plug)
            if interface is None:
                skipped.append({"snap": snap_name, "plug": plug,
                                "reason": "plug not in current connections"})
                continue
            if not _slot_exists(current_connections, slot_snap, slot):
                skipped.append({"snap": snap_name, "plug": plug,
                                "reason": "slot not in current connections"})
                continue
            tier = interfaces.tier_for(interface)
            if tier == 3:
                not_restored.append(
                    {"snap": snap_name, "plug": plug,
                     "command": "snap connect %s:%s %s:%s"
                                % (snap_name, plug, slot_snap, slot)})
            else:
                steps.append({"action": "connect", "plug_snap": snap_name,
                              "plug": plug, "slot_snap": slot_snap,
                              "slot": slot, "tier": tier})
        else:
            key = by_plug[plug]
            if key != (plug, slot_snap, slot):
                c = current[key]
                steps.append({"action": "disconnect",
                              "plug_snap": snap_name, "plug": plug,
                              "slot_snap": key[1], "slot": key[2],
                              "tier": interfaces.tier_for(
                                  str(c.get("interface") or "?"))})
                if _slot_exists(current_connections, slot_snap, slot):
                    interface = _interface_of(current_connections,
                                              snap_name, plug)
                    tier = interfaces.tier_for(interface or "?")
                    steps.append({"action": "connect", "plug_snap": snap_name,
                                  "plug": plug, "slot_snap": slot_snap,
                                  "slot": slot, "tier": tier})
                else:
                    skipped.append({"snap": snap_name, "plug": plug,
                                    "reason": "slot not in current "
                                              "connections"})
    for key, c in current.items():
        plug = key[0]
        in_baseline = any(
            isinstance(e, dict) and e.get("plug") == plug
            for e in (baseline_entry or {}).get("connected") or [])
        if not in_baseline and c.get("manual"):
            steps.append({"action": "disconnect", "plug_snap": snap_name,
                          "plug": plug, "slot_snap": key[1], "slot": key[2],
                          "tier": interfaces.tier_for(
                              str(c.get("interface") or "?"))})
    return {"steps": steps, "skipped": skipped, "not_restored": not_restored}


def changed_snaps(baselines, connections):
    """{snap: step count} for snaps whose diff has steps."""
    changed = {}
    for snap, entry in baselines.items():
        diff = compute_diff(entry, connections, snap)
        if diff["steps"]:
            changed[snap] = len(diff["steps"])
    return changed
