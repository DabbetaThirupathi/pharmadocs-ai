"""Deviation report drafting assistant.

Turns a free-text incident description into a structured draft deviation
report (ICH Q9 / Q10 style): classification, impact assessment, immediate
actions, 5-Why root-cause skeleton, related SOPs and CAPA suggestions.
The draft is ALWAYS marked for QA review — the assistant never closes a deviation.
"""
import json
import re
from datetime import datetime, timezone

CRITICAL_TERMS = ["sterility", "sterile breach", "contamination", "microbial", "endotoxin",
                  "patient", "mix-up", "mixup", "wrong label", "glass particle", "particulate",
                  "integrity failure", "media fill"]
MAJOR_TERMS = ["out of specification", "oos", "excursion", "failure", "failed", "breakdown",
               "temperature", "pressure", "alarm", "not recorded", "missed", "expired",
               "calibration", "differential pressure", "leak"]

AREA_KEYWORDS = {
    "Aseptic Filling": ["filling", "vial", "stopper", "lyophil", "rabs", "isolator", "aseptic"],
    "Sterilization": ["autoclave", "steriliz", "sterilis", "depyrogen", "tunnel"],
    "Warehouse": ["warehouse", "receipt", "dispensing", "storage", "cold room", "pallet"],
    "Quality Control Lab": ["hplc", "assay", "lab", "sample", "analysis", "oos"],
    "Cleanroom / Environment": ["environmental", "particle count", "settle plate", "grade a",
                                "grade b", "differential pressure", "hvac", "gowning"],
    "Packaging": ["label", "carton", "packing", "serialization", "artwork"],
}

CAPA_LIBRARY = {
    "Aseptic Filling": ["Re-train operators on aseptic behaviour and interventions",
                        "Review filling-line intervention list and media-fill coverage"],
    "Sterilization": ["Verify sterilizer cycle parameters against the validated cycle",
                      "Review preventive maintenance and calibration status of the sterilizer"],
    "Warehouse": ["Strengthen FEFO checks at dispensing", "Review temperature-mapping data for storage area"],
    "Quality Control Lab": ["Perform Phase-I lab investigation per OOS SOP",
                            "Verify instrument calibration and analyst qualification"],
    "Cleanroom / Environment": ["Trend EM data for the room over the last 30 days",
                                "Verify HVAC/AHU performance and filter integrity"],
    "Packaging": ["Introduce double verification for label reconciliation",
                  "Review line-clearance checklist"],
}


def classify(text: str):
    t = text.lower()
    hits_c = [w for w in CRITICAL_TERMS if w in t]
    hits_m = [w for w in MAJOR_TERMS if w in t]
    if hits_c:
        return "CRITICAL", hits_c
    if hits_m:
        return "MAJOR", hits_m
    return "MINOR", []


def detect_area(text: str):
    t = text.lower()
    scores = {a: sum(1 for k in kws if k in t) for a, kws in AREA_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "General"


def extract_batch(text: str):
    m = re.search(r"\b(?:batch|lot|b\.?\s?no\.?)\s*[:#]?\s*([A-Z0-9\-]{4,})", text, re.I)
    return m.group(1).upper() if m else None


def next_dev_number(conn):
    year = datetime.now(timezone.utc).year
    row = conn.execute("SELECT COUNT(*) AS n FROM deviations WHERE dev_number LIKE ?",
                       (f"DEV-{year}-%",)).fetchone()
    return f"DEV-{year}-{row['n'] + 1:04d}"


def draft_report(conn, description: str, retriever, access_levels, user_id=None):
    classification, triggers = classify(description)
    area = detect_area(description)
    batch = extract_batch(description)
    related = retriever.search(description, access_levels, k=3)

    report = {
        "dev_number": next_dev_number(conn),
        "status": "DRAFT – PENDING QA REVIEW",
        "reported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "area": area,
        "batch_number": batch,
        "classification": classification,
        "classification_triggers": triggers,
        "description": description.strip(),
        "impact_assessment": {
            "product_quality": "To be assessed by QA" if classification != "MINOR" else "No direct impact expected",
            "patient_safety": "Potential – escalate immediately" if classification == "CRITICAL" else "Not expected",
            "batch_disposition": "HOLD batch pending investigation" if batch and classification != "MINOR" else "N/A",
            "other_batches": "Check batches processed on the same equipment/area",
        },
        "immediate_actions": [
            "Stop/segregate affected material and label as 'UNDER INVESTIGATION'",
            "Inform shift supervisor and QA within 1 hour" if classification == "CRITICAL"
            else "Inform shift supervisor and QA within the shift",
            "Preserve evidence: records, printouts, samples, photos",
        ],
        "root_cause_5_why": [{"why": i + 1, "question": q, "answer": ""} for i, q in enumerate([
            "Why did the event occur?",
            "Why did that condition exist?",
            "Why was it not detected earlier?",
            "Why did the existing control not prevent it?",
            "Why was the control designed/implemented that way?",
        ])],
        "related_sops": [{"doc_number": h.doc_number, "title": h.title, "version": h.version,
                          "section": h.section} for h in related],
        "suggested_capa": CAPA_LIBRARY.get(area, ["Perform risk assessment and define CAPA with QA"]),
    }
    conn.execute(
        """INSERT INTO deviations (dev_number, created_by, area, classification, description, report_json)
           VALUES (?,?,?,?,?,?)""",
        (report["dev_number"], user_id, area, classification, description, json.dumps(report)),
    )
    conn.commit()
    return report
