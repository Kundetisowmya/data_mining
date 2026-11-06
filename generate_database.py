#!/usr/bin/env python3
"""Build a deterministic, entirely synthetic primary-care diabetes database.

Only Python's standard library is required. The fixed default seed makes the
same records and deliberately seeded quality defects reproducible.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sqlite3
from pathlib import Path

SEED_DEFAULT = 20261007
PATIENT_COUNT = 1200
DATA_CUT_OFF = dt.date(2026, 10, 31)
MISSING_EMAIL_COUNT = 36
MISSING_SMOKING_COUNT = 60
MISSING_ETHNICITY_COUNT = 36
DUPLICATE_EMAIL_PAIRS = 12
DUPLICATE_OBSERVATION_ROWS = 36

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE clinic (
    clinic_id TEXT PRIMARY KEY,
    clinic_name TEXT NOT NULL UNIQUE,
    locality TEXT NOT NULL,
    region_code TEXT NOT NULL,
    opened_on TEXT NOT NULL CHECK (date(opened_on) IS NOT NULL)
);
CREATE TABLE clinician (
    clinician_id TEXT PRIMARY KEY,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('GP', 'nurse', 'pharmacist', 'healthcare_assistant')),
    specialty TEXT NOT NULL,
    started_on TEXT NOT NULL CHECK (date(started_on) IS NOT NULL)
);
CREATE TABLE clinician_site (
    clinician_id TEXT NOT NULL,
    clinic_id TEXT NOT NULL,
    valid_from TEXT NOT NULL CHECK (date(valid_from) IS NOT NULL),
    PRIMARY KEY (clinician_id, clinic_id, valid_from),
    UNIQUE (clinician_id, clinic_id),
    FOREIGN KEY (clinician_id) REFERENCES clinician(clinician_id) ON DELETE RESTRICT,
    FOREIGN KEY (clinic_id) REFERENCES clinic(clinic_id) ON DELETE RESTRICT
);
CREATE TABLE patient (
    patient_id TEXT PRIMARY KEY,
    synthetic_ref TEXT NOT NULL UNIQUE,
    home_clinic_id TEXT NOT NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    date_of_birth TEXT NOT NULL CHECK (date(date_of_birth) IS NOT NULL),
    sex_at_birth TEXT NOT NULL CHECK (sex_at_birth IN ('female', 'male', 'intersex', 'unknown')),
    ethnicity TEXT CHECK (ethnicity IS NULL OR ethnicity IN ('Asian', 'Black', 'Mixed', 'White', 'Other')),
    postcode_area TEXT NOT NULL,
    contact_email TEXT CHECK (contact_email IS NULL OR
        (instr(contact_email, '@') > 1 AND substr(contact_email, -8) = '.invalid')),
    smoking_status TEXT CHECK (smoking_status IS NULL OR smoking_status IN ('never', 'former', 'current')),
    registered_on TEXT NOT NULL CHECK (date(registered_on) IS NOT NULL),
    UNIQUE (patient_id, home_clinic_id),
    FOREIGN KEY (home_clinic_id) REFERENCES clinic(clinic_id) ON DELETE RESTRICT
);
CREATE TABLE condition_catalog (
    condition_id TEXT PRIMARY KEY,
    condition_name TEXT NOT NULL UNIQUE,
    icd10_code TEXT NOT NULL UNIQUE,
    chronic INTEGER NOT NULL CHECK (chronic IN (0, 1))
);
CREATE TABLE patient_condition (
    patient_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    diagnosed_on TEXT NOT NULL CHECK (date(diagnosed_on) IS NOT NULL),
    PRIMARY KEY (patient_id, condition_id, diagnosed_on),
    FOREIGN KEY (patient_id) REFERENCES patient(patient_id) ON DELETE RESTRICT,
    FOREIGN KEY (condition_id) REFERENCES condition_catalog(condition_id) ON DELETE RESTRICT
);
CREATE TABLE medication_catalog (
    medication_code TEXT NOT NULL,
    strength_mg INTEGER NOT NULL CHECK (strength_mg > 0),
    generic_name TEXT NOT NULL,
    route TEXT NOT NULL CHECK (route IN ('oral', 'injection')),
    drug_class TEXT NOT NULL,
    PRIMARY KEY (medication_code, strength_mg)
);
CREATE TABLE appointment (
    appointment_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL,
    clinic_id TEXT NOT NULL,
    clinician_id TEXT NOT NULL,
    scheduled_at TEXT NOT NULL CHECK (datetime(scheduled_at) IS NOT NULL),
    status TEXT NOT NULL CHECK (status IN ('attended', 'did_not_attend', 'cancelled')),
    appointment_type TEXT NOT NULL CHECK (appointment_type IN
        ('diabetes_review', 'medication_review', 'routine_follow_up', 'telephone_review')),
    UNIQUE (appointment_id, patient_id),
    FOREIGN KEY (patient_id, clinic_id) REFERENCES patient(patient_id, home_clinic_id) ON DELETE RESTRICT,
    FOREIGN KEY (clinician_id, clinic_id) REFERENCES clinician_site(clinician_id, clinic_id) ON DELETE RESTRICT
);
CREATE TABLE observation (
    observation_id TEXT PRIMARY KEY,
    appointment_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    metric_code TEXT NOT NULL CHECK (metric_code IN
        ('HBA1C', 'BMI', 'SBP', 'DBP', 'CREATININE', 'CHOLESTEROL')),
    observed_on TEXT NOT NULL CHECK (date(observed_on) IS NOT NULL),
    value_num REAL NOT NULL,
    unit TEXT NOT NULL,
    CHECK (
        (metric_code = 'HBA1C' AND unit = 'mmol/mol' AND value_num BETWEEN 20 AND 200) OR
        (metric_code = 'BMI' AND unit = 'kg/m2' AND value_num BETWEEN 10 AND 80) OR
        (metric_code IN ('SBP', 'DBP') AND unit = 'mmHg' AND value_num BETWEEN 30 AND 260) OR
        (metric_code = 'CREATININE' AND unit = 'umol/L' AND value_num BETWEEN 10 AND 2000) OR
        (metric_code = 'CHOLESTEROL' AND unit = 'mmol/L' AND value_num BETWEEN 0.5 AND 40)
    ),
    FOREIGN KEY (appointment_id, patient_id) REFERENCES appointment(appointment_id, patient_id) ON DELETE RESTRICT
);
CREATE TABLE prescription (
    patient_id TEXT NOT NULL,
    medication_code TEXT NOT NULL,
    strength_mg INTEGER NOT NULL,
    prescribed_on TEXT NOT NULL CHECK (date(prescribed_on) IS NOT NULL),
    discontinued_on TEXT CHECK (discontinued_on IS NULL OR date(discontinued_on) IS NOT NULL),
    status TEXT NOT NULL CHECK (status IN ('active', 'stopped')),
    daily_dose TEXT NOT NULL,
    PRIMARY KEY (patient_id, medication_code, strength_mg, prescribed_on),
    CHECK ((status = 'active' AND discontinued_on IS NULL) OR
           (status = 'stopped' AND discontinued_on IS NOT NULL AND discontinued_on >= prescribed_on)),
    FOREIGN KEY (patient_id) REFERENCES patient(patient_id) ON DELETE RESTRICT,
    FOREIGN KEY (medication_code, strength_mg)
        REFERENCES medication_catalog(medication_code, strength_mg) ON DELETE RESTRICT
);
CREATE TABLE dataset_metadata (
    metadata_key TEXT PRIMARY KEY,
    metadata_value TEXT NOT NULL
);
CREATE INDEX idx_appointment_patient_date ON appointment(patient_id, scheduled_at);
CREATE INDEX idx_appointment_clinic_status ON appointment(clinic_id, status);
CREATE INDEX idx_observation_patient_metric_date ON observation(patient_id, metric_code, observed_on);
CREATE INDEX idx_patient_condition_condition ON patient_condition(condition_id, patient_id);
CREATE INDEX idx_prescription_patient_date ON prescription(patient_id, prescribed_on);
"""

CLINICS = [
    ("CL01", "Northgate Health Centre", "Northgate", "RG1", "1998-04-01"),
    ("CL02", "Riverside Primary Care", "Riverside", "RG2", "2003-06-15"),
    ("CL03", "Meadow Park Surgery", "Meadow Park", "RG3", "1995-10-01"),
    ("CL04", "Harbour View Clinic", "Harbour View", "RG4", "2010-02-01"),
    ("CL05", "Central Lane Practice", "Central Lane", "RG5", "2007-09-01"),
    ("CL06", "Hilltop Medical Centre", "Hilltop", "RG6", "2001-01-01"),
]
FIRST_NAMES = ["Aisha", "Amir", "Anita", "Daniel", "Eleanor", "Farah", "George", "Hannah",
               "Imran", "James", "Kavita", "Leah", "Luca", "Maya", "Mohammed", "Nadia",
               "Oliver", "Priya", "Ravi", "Sara", "Sophie", "Thomas", "Yasmin", "Zara"]
LAST_NAMES = ["Ahmed", "Ali", "Baker", "Brown", "Campbell", "Chen", "Clarke", "Davies",
              "Evans", "Garcia", "Hughes", "Khan", "Lewis", "Malik", "Martin", "Patel",
              "Roberts", "Shah", "Singh", "Taylor", "Walker", "Wilson", "Wong", "Young"]
ETHNICITIES = ["Asian", "Black", "Mixed", "White", "Other"]
ETHNICITY_WEIGHTS = [0.16, 0.08, 0.05, 0.65, 0.06]
CONDITIONS = [
    ("C01", "Type 2 diabetes", "E11", 1),
    ("C02", "Hypertension", "I10", 1),
    ("C03", "Dyslipidaemia", "E78.5", 1),
    ("C04", "Chronic kidney disease", "N18", 1),
    ("C05", "Obesity", "E66.9", 1),
    ("C06", "Diabetic retinopathy", "E11.3", 1),
]
MEDICATIONS = [
    ("METFORMIN", 500, "metformin", "oral", "biguanide"),
    ("METFORMIN", 1000, "metformin", "oral", "biguanide"),
    ("GLICLAZIDE", 80, "gliclazide", "oral", "sulfonylurea"),
    ("DAPAGLIFLOZIN", 10, "dapagliflozin", "oral", "SGLT2 inhibitor"),
    ("EMPAGLIFLOZIN", 10, "empagliflozin", "oral", "SGLT2 inhibitor"),
    ("ATORVASTATIN", 20, "atorvastatin", "oral", "statin"),
    ("LISINOPRIL", 10, "lisinopril", "oral", "ACE inhibitor"),
    ("SEMAGLUTIDE", 1, "semaglutide", "injection", "GLP-1 receptor agonist"),
]
OBS_META = {
    "HBA1C": ("mmol/mol", 20, 200),
    "BMI": ("kg/m2", 10, 80),
    "SBP": ("mmHg", 30, 260),
    "DBP": ("mmHg", 30, 260),
    "CREATININE": ("umol/L", 10, 2000),
    "CHOLESTEROL": ("mmol/L", 0.5, 40),
}

def as_date(value):
    return dt.date.fromisoformat(value) if isinstance(value, str) else value

def random_date(rng, start, end):
    start, end = as_date(start), as_date(end)
    span = max(0, (end - start).days)
    return start + dt.timedelta(days=rng.randint(0, span))

def clipped_gauss(rng, mean, sd, low, high, digits=1):
    return round(min(high, max(low, rng.gauss(mean, sd))), digits)

def build_database(db_path, seed=SEED_DEFAULT):
    rng = random.Random(seed)
    target = Path(db_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".building")
    if staging.exists():
        staging.unlink()
    try:
        conn = sqlite3.connect(staging)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        cur = conn.cursor()
        cur.executemany("INSERT INTO clinic VALUES (?, ?, ?, ?, ?)", CLINICS)
        cur.executemany("INSERT INTO condition_catalog VALUES (?, ?, ?, ?)", CONDITIONS)
        cur.executemany("INSERT INTO medication_catalog VALUES (?, ?, ?, ?, ?)", MEDICATIONS)

        roles = ["GP", "nurse", "pharmacist", "healthcare_assistant"]
        clinician_names = []
        clinician_sites = {}
        for ci, clinic in enumerate(CLINICS):
            for j in range(5):
                clinician_id = f"D{ci * 5 + j + 1:03d}"
                first = FIRST_NAMES[(ci * 5 + j * 3 + 2) % len(FIRST_NAMES)]
                last = LAST_NAMES[(ci * 7 + j * 5 + 1) % len(LAST_NAMES)]
                role = roles[(j + ci) % len(roles)]
                specialty = "diabetes care" if role in ("nurse", "GP", "pharmacist") else "primary care"
                started = random_date(rng, "2008-01-01", "2022-12-31").isoformat()
                clinician_names.append((clinician_id, first, last, role, specialty, started))
                clinician_sites[clinic[0]] = clinician_sites.get(clinic[0], []) + [clinician_id]
        cur.executemany("INSERT INTO clinician VALUES (?, ?, ?, ?, ?, ?)", clinician_names)
        cur.executemany(
            "INSERT INTO clinician_site VALUES (?, ?, ?)",
            [(c[0], clinic[0], "2023-01-01") for clinic in CLINICS for c in clinician_names
             if c[0] in clinician_sites[clinic[0]]],
        )

        missing_email_ids = set(rng.sample(range(1, PATIENT_COUNT + 1), MISSING_EMAIL_COUNT))
        remaining = [i for i in range(1, PATIENT_COUNT + 1) if i not in missing_email_ids]
        dup_email_ids = set(rng.sample(remaining, DUPLICATE_EMAIL_PAIRS * 2))
        missing_smoking_ids = set(rng.sample(range(1, PATIENT_COUNT + 1), MISSING_SMOKING_COUNT))
        missing_ethnicity_ids = set(rng.sample(range(1, PATIENT_COUNT + 1), MISSING_ETHNICITY_COUNT))
        email_pair_ids = sorted(dup_email_ids)
        email_by_id = {}
        for k in range(DUPLICATE_EMAIL_PAIRS):
            shared = f"shared.contact.{k + 1:02d}@example.invalid"
            email_by_id[email_pair_ids[2 * k]] = shared
            email_by_id[email_pair_ids[2 * k + 1]] = shared

        patients = []
        patient_a1c_baseline = {}
        registration_by_id = {}
        for i in range(1, PATIENT_COUNT + 1):
            patient_id = f"P{i:06d}"
            clinic_id = CLINICS[(i * 17 + rng.randrange(len(CLINICS))) % len(CLINICS)][0]
            first = rng.choice(FIRST_NAMES)
            last = rng.choice(LAST_NAMES)
            age = round(min(89, max(18, rng.gauss(63, 14))))
            dob = DATA_CUT_OFF - dt.timedelta(days=int(age * 365.2425 + rng.randrange(365)))
            sex = rng.choices(["female", "male", "intersex", "unknown"], [0.49, 0.49, 0.005, 0.015])[0]
            ethnicity = None if i in missing_ethnicity_ids else rng.choices(ETHNICITIES, ETHNICITY_WEIGHTS)[0]
            postcode_area = CLINICS[int(clinic_id[2:]) - 1][3]
            if i in missing_email_ids:
                email = None
            elif i in email_by_id:
                email = email_by_id[i]
            else:
                email = f"{first.lower()}.{last.lower()}.{i:06d}@example.invalid"
            smoking = None if i in missing_smoking_ids else rng.choices(
                ["never", "former", "current"], [0.47, 0.39, 0.14]
            )[0]
            registered = random_date(rng, "2015-01-01", DATA_CUT_OFF).isoformat()
            registration_by_id[patient_id] = registered
            patients.append((patient_id, f"SYN-{i:06d}", clinic_id, first, last,
                             dob.isoformat(), sex, ethnicity, postcode_area, email,
                             smoking, registered))
            patient_a1c_baseline[patient_id] = clipped_gauss(rng, 59, 15, 35, 125)
        cur.executemany("""INSERT INTO patient
            (patient_id, synthetic_ref, home_clinic_id, first_name, last_name, date_of_birth,
             sex_at_birth, ethnicity, postcode_area, contact_email, smoking_status, registered_on)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", patients)

        patient_conditions = []
        condition_probabilities = [("C02", 0.53), ("C03", 0.46), ("C04", 0.18),
                                   ("C05", 0.31), ("C06", 0.10)]
        for patient_id, *_ in patients:
            registered = as_date(registration_by_id[patient_id])
            patient_conditions.append((patient_id, "C01", random_date(rng, registered, DATA_CUT_OFF).isoformat()))
            for condition_id, probability in condition_probabilities:
                if rng.random() < probability:
                    patient_conditions.append((patient_id, condition_id,
                                               random_date(rng, registered, DATA_CUT_OFF).isoformat()))
        cur.executemany("INSERT INTO patient_condition VALUES (?, ?, ?)", patient_conditions)

        appointment_rows = []
        appointment_statuses = ["attended", "did_not_attend", "cancelled"]
        for patient_id, _, clinic_id, *_ in patients:
            count = rng.randint(2, 8)
            providers = clinician_sites[clinic_id]
            for _ in range(count):
                appointment_id = f"A{len(appointment_rows) + 1:07d}"
                appt_day = random_date(rng, "2023-01-01", DATA_CUT_OFF)
                hour = rng.choice([8, 9, 10, 11, 13, 14, 15, 16])
                minute = rng.choice([0, 15, 30, 45])
                stamp = dt.datetime.combine(appt_day, dt.time(hour, minute)).isoformat(sep=" ")
                status = rng.choices(appointment_statuses, [0.82, 0.08, 0.10])[0]
                appt_type = rng.choices(
                    ["diabetes_review", "medication_review", "routine_follow_up", "telephone_review"],
                    [0.43, 0.22, 0.23, 0.12],
                )[0]
                clinician_id = rng.choice(providers)
                appointment_rows.append((appointment_id, patient_id, clinic_id, clinician_id,
                                         stamp, status, appt_type))
        cur.executemany("INSERT INTO appointment VALUES (?, ?, ?, ?, ?, ?, ?)", appointment_rows)

        observation_rows = []
        for appointment_id, patient_id, clinic_id, clinician_id, stamp, status, appt_type in appointment_rows:
            if status != "attended":
                continue
            measured_on = stamp[:10]
            measures = []
            if rng.random() < 0.78:
                measures.append(("HBA1C", clipped_gauss(rng, patient_a1c_baseline[patient_id], 6, 30, 140), "mmol/mol"))
            if rng.random() < 0.34:
                measures.append(("BMI", clipped_gauss(rng, 31, 6, 16, 58), "kg/m2"))
            if rng.random() < 0.55:
                sbp = clipped_gauss(rng, 134, 18, 85, 205)
                dbp = clipped_gauss(rng, 79, 11, 48, 125)
                measures.extend([("SBP", sbp, "mmHg"), ("DBP", dbp, "mmHg")])
            if rng.random() < 0.22:
                measures.append(("CREATININE", clipped_gauss(rng, 92, 42, 30, 380), "umol/L"))
            if rng.random() < 0.18:
                measures.append(("CHOLESTEROL", clipped_gauss(rng, 4.8, 1.2, 2, 10), "mmol/L"))
            for metric, value, unit in measures:
                observation_id = f"O{len(observation_rows) + 1:07d}"
                observation_rows.append((observation_id, appointment_id, patient_id,
                                         metric, measured_on, value, unit))
        original_observations = list(observation_rows)
        duplicate_source_rows = rng.sample(original_observations, DUPLICATE_OBSERVATION_ROWS)
        for source in duplicate_source_rows:
            observation_id = f"O{len(observation_rows) + 1:07d}"
            observation_rows.append((observation_id, *source[1:]))
        cur.executemany("INSERT INTO observation VALUES (?, ?, ?, ?, ?, ?, ?)", observation_rows)

        prescription_rows = []
        meds_by_code = {(m[0], m[1]): m for m in MEDICATIONS}
        choices = list(meds_by_code)
        for patient_id, *_ in patients:
            if rng.random() > 0.91:
                continue
            selected = rng.sample(choices, rng.choices([1, 2, 3], [0.42, 0.43, 0.15])[0])
            registered = max(as_date(registration_by_id[patient_id]), dt.date(2023, 1, 1))
            for medication_code, strength in selected:
                prescribed_on = random_date(rng, registered, DATA_CUT_OFF)
                stopped = rng.random() < 0.18
                discontinued = random_date(rng, prescribed_on, DATA_CUT_OFF).isoformat() if stopped else None
                status = "stopped" if stopped else "active"
                dose = rng.choice(["once daily", "twice daily", "as directed"])
                prescription_rows.append((patient_id, medication_code, strength,
                                          prescribed_on.isoformat(), discontinued, status, dose))
        cur.executemany("INSERT INTO prescription VALUES (?, ?, ?, ?, ?, ?, ?)", prescription_rows)

        metadata = [
            ("topic", "Synthetic primary-care type 2 diabetes cohort"),
            ("seed", str(seed)),
            ("data_cut_off", DATA_CUT_OFF.isoformat()),
            ("patient_count", str(PATIENT_COUNT)),
            ("missingness_policy", "36 missing emails; 60 missing smoking statuses; 36 missing ethnicity values"),
            ("duplicate_policy", "12 shared contact emails across 24 patients; 36 cloned observation rows"),
            ("synthetic_notice", "No real people, records, clinics, or contact details are represented"),
        ]
        cur.executemany("INSERT INTO dataset_metadata VALUES (?, ?)", metadata)
        conn.commit()
        validation = validate_database(conn)
        conn.close()
        staging.replace(target)
        validation["database_path"] = str(target)
        validation["seed"] = seed
        return validation
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        if staging.exists():
            staging.unlink()
        raise

def validate_database(conn):
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise AssertionError("SQLite foreign-key enforcement is disabled")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise AssertionError(f"SQLite integrity_check returned {integrity!r}")
    fk_issues = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk_issues:
        raise AssertionError(f"Foreign-key violations: {fk_issues[:5]}")
    table_names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    row_counts = {name: conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in table_names}
    if row_counts.get("patient", 0) < 1000:
        raise AssertionError("At least one table must contain 1,000 rows")
    q = lambda sql: conn.execute(sql).fetchone()[0]
    audit = {
        "missing_contact_email": q("SELECT count(*) FROM patient WHERE contact_email IS NULL"),
        "missing_smoking_status": q("SELECT count(*) FROM patient WHERE smoking_status IS NULL"),
        "missing_ethnicity": q("SELECT count(*) FROM patient WHERE ethnicity IS NULL"),
        "duplicate_email_values": q("""SELECT count(*) FROM (
            SELECT contact_email FROM patient WHERE contact_email IS NOT NULL
            GROUP BY contact_email HAVING count(*) > 1)"""),
        "patients_in_duplicate_email_groups": q("""SELECT coalesce(sum(n), 0) FROM (
            SELECT count(*) AS n FROM patient WHERE contact_email IS NOT NULL
            GROUP BY contact_email HAVING count(*) > 1)"""),
        "duplicate_observation_groups": q("""SELECT count(*) FROM (
            SELECT appointment_id, patient_id, metric_code, observed_on, value_num, unit
            FROM observation GROUP BY appointment_id, patient_id, metric_code, observed_on, value_num, unit
            HAVING count(*) > 1)"""),
        "extra_duplicate_observation_rows": q("""SELECT coalesce(sum(n - 1), 0) FROM (
            SELECT count(*) AS n FROM observation
            GROUP BY appointment_id, patient_id, metric_code, observed_on, value_num, unit HAVING count(*) > 1)"""),
    }
    expected = {
        "missing_contact_email": MISSING_EMAIL_COUNT,
        "missing_smoking_status": MISSING_SMOKING_COUNT,
        "missing_ethnicity": MISSING_ETHNICITY_COUNT,
        "duplicate_email_values": DUPLICATE_EMAIL_PAIRS,
        "patients_in_duplicate_email_groups": DUPLICATE_EMAIL_PAIRS * 2,
        "duplicate_observation_groups": DUPLICATE_OBSERVATION_ROWS,
        "extra_duplicate_observation_rows": DUPLICATE_OBSERVATION_ROWS,
    }
    if audit != expected:
        raise AssertionError(f"Quality audit mismatch: actual={audit}, expected={expected}")
    probes = constraint_probes(conn)
    return {"integrity_check": integrity, "foreign_key_violations": len(fk_issues),
            "constraint_probes_rejected": probes, "row_counts": row_counts, "quality_audit": audit}

def constraint_probes(conn):
    """Confirm representative CHECK, UNIQUE, NOT NULL, composite-key and FK rules reject bad rows."""
    valid = conn.execute("""SELECT patient_id, home_clinic_id, synthetic_ref, first_name, last_name,
        date_of_birth, ethnicity, postcode_area, contact_email, smoking_status, registered_on
        FROM patient LIMIT 1""").fetchone()
    p_id, clinic_id, syn_ref, first, last, dob, ethnicity, area, email, smoking, registered = valid
    condition = conn.execute("SELECT condition_id, diagnosed_on FROM patient_condition LIMIT 1").fetchone()
    observation = conn.execute("SELECT appointment_id, patient_id, observed_on, value_num, unit FROM observation LIMIT 1").fetchone()
    tests = [
        ("CHECK", """INSERT INTO patient
          (patient_id, synthetic_ref, home_clinic_id, first_name, last_name, date_of_birth,
           sex_at_birth, ethnicity, postcode_area, contact_email, smoking_status, registered_on)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
         ("PX-CHECK", "SYN-X", clinic_id, "Test", "Patient", dob, "invalid", ethnicity, area, None, None, registered)),
        ("UNIQUE", """INSERT INTO patient
          (patient_id, synthetic_ref, home_clinic_id, first_name, last_name, date_of_birth,
           sex_at_birth, ethnicity, postcode_area, contact_email, smoking_status, registered_on)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
         ("PX-UNIQUE", syn_ref, clinic_id, "Test", "Patient", dob, "unknown", ethnicity, area, None, None, registered)),
        ("NOT NULL", "INSERT INTO clinic (clinic_id, clinic_name, locality, region_code, opened_on) VALUES (?, ?, ?, ?, ?)",
         ("CL-X", None, "Test", "RGX", "2020-01-01")),
        ("FOREIGN KEY", "INSERT INTO patient_condition VALUES (?, ?, ?)",
         ("PX-FOREIGN", condition[0], condition[1])),
        ("COMPOSITE PRIMARY KEY", "INSERT INTO patient_condition VALUES (?, ?, ?)",
         (p_id, condition[0], condition[1])),
        ("MEASUREMENT CHECK", "INSERT INTO observation VALUES (?, ?, ?, ?, ?, ?, ?)",
         ("OX-CHECK", observation[0], observation[1], "INVALID", observation[2], observation[3], observation[4])),
    ]
    rejected = []
    for name, sql, values in tests:
        conn.execute("SAVEPOINT constraint_probe")
        try:
            conn.execute(sql, values)
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK TO constraint_probe")
            conn.execute("RELEASE constraint_probe")
            rejected.append(name)
        else:
            conn.execute("ROLLBACK TO constraint_probe")
            conn.execute("RELEASE constraint_probe")
            raise AssertionError(f"Constraint probe was unexpectedly accepted: {name}")
    return rejected

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="synthetic_diabetes.db", help="SQLite file to create or replace")
    parser.add_argument("--seed", type=int, default=SEED_DEFAULT, help="random seed (default: %(default)s)")
    args = parser.parse_args()
    result = build_database(args.db_path, args.seed)
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
