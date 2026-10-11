"""Prepare Score layers for synthesis and summarize unresolved singing units."""


def lyric_order_conflict_unit_ids(layers: dict) -> set[str]:
    prefix = "spoken_fallback_order_conflict:"
    return {item[len(prefix):] for item in layers.get("diagnostics", [])
            if isinstance(item, str) and item.startswith(prefix)}


def omit_unresolved_synthesis_units(layers: dict) -> int:
    """Turn unresolved Stage 3 units into explicit, provenance-backed omissions."""
    unresolved = list(dict.fromkeys(layers.get("unresolved_unit_ids", [])))
    if not unresolved:
        return 0
    units = {item["singing_unit_id"] for item in layers.get("performed", [])}
    rendered = {item["singing_unit_id"] for item in layers.get("synthesis_plan", [])}
    existing = {item["singing_unit_id"] for item in layers.get("omissions", [])}
    if any(unit_id not in units or unit_id in rendered for unit_id in unresolved):
        raise ValueError("Stage 3の未解決歌唱単位が合成計画と矛盾しています")

    evidence = list(layers.get("evidence", []))
    layers["evidence"] = evidence
    occupied = {item["id"] for item in evidence}
    omissions = list(layers.get("omissions", []))
    layers["omissions"] = omissions
    order_conflicts = lyric_order_conflict_unit_ids(layers)
    for unit_id in unresolved:
        if unit_id in existing:
            continue
        base = f"stage3-synthesis-omission-{unit_id}"
        evidence_id = base
        suffix = 1
        while evidence_id in occupied:
            evidence_id = f"{base}-{suffix}"
            suffix += 1
        occupied.add(evidence_id)
        evidence.append({
            "id": evidence_id,
            "source": "stage3",
            "kind": "synthesis-omission",
            "confidence": 1.0,
            "detail": {"reason": "lyric-order-conflict" if unit_id in order_conflicts
                       else "pitch-unresolved"},
        })
        omissions.append({
            "singing_unit_id": unit_id,
            "reason": ("歌詞の順序と発音時刻が矛盾するため合成から省略"
                       if unit_id in order_conflicts
                       else "Stage 3で音高を確定できないため合成から省略"),
            "evidence_ids": [evidence_id],
        })
    layers["unresolved_unit_ids"] = []
    diagnostics = list(layers.get("diagnostics", []))
    layers["diagnostics"] = diagnostics
    diagnostics.append({
        "stage": "stage3",
        "status": "synthesis-omission",
        "unit_count": len(unresolved),
    })
    return len(unresolved)


def generation_quality_assessment(layers: dict) -> dict[str, object]:
    """Summarize whether missing melody evidence affects much of the song."""
    performed = {
        item.get("singing_unit_id")
        for item in layers.get("performed", [])
        if item.get("singing_unit_id")
    }
    unsupported = {
        item.get("singing_unit_id")
        for item in layers.get("synthesis_plan", [])
        if item.get("singing_unit_id")
        and "sheetsage2-vocal" not in item.get("pitch_sources", [])
    }
    unsupported.update(
        item.get("singing_unit_id")
        for item in layers.get("omissions", [])
        if item.get("singing_unit_id")
    )
    affected = len(unsupported & performed)
    total = len(performed)
    affected_ratio = affected / total if total else 0.0
    # One isolated gap should not condemn a whole song. Warn when melody evidence
    # is absent for multiple units and a material share of the performance.
    # A lyric-order conflict always needs review, even for one omitted unit.
    order_conflicts = lyric_order_conflict_unit_ids(layers) & {
        item.get("singing_unit_id") for item in layers.get("omissions", [])
    }
    warning = bool(order_conflicts) or (affected >= 2 and affected_ratio >= 0.2)
    return {
        "status": "warning" if warning else "ok",
        "reason": ("lyric-order-conflict" if order_conflicts else
                   "insufficient-melody-coverage" if warning else None),
        "affected_singing_units": affected,
        "total_singing_units": total,
        "affected_ratio": round(affected_ratio, 4),
    }
