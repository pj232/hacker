# -*- coding: utf-8 -*-
"""
Screenlead — collecte, analyse et gestion des données de chantiers (génie civil).

Lancement :   streamlit run app.py

Variables d'environnement (facultatives) :
    SCREENLEAD_DB              chemin de la base SQLite        (défaut : screenlead.db)
    SCREENLEAD_DEMO            "1" = données de démonstration  (défaut : 1)
    SCREENLEAD_ADMIN_PASSWORD  mot de passe initial de l'admin si DEMO=0
    SCREENLEAD_DEVISE          devise affichée                 (défaut : FCFA)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import random
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
DB_PATH = Path(os.environ.get("SCREENLEAD_DB", "screenlead.db"))
DEMO = os.environ.get("SCREENLEAD_DEMO", "1") == "1"
DEVISE = os.environ.get("SCREENLEAD_DEVISE", "FCFA")

ROLES = {
    "maitre_ouvrage": "Maître d'ouvrage",
    "chef_chantier": "Chef de chantier",
    "ingenieur": "Ingénieur",
    "admin": "Administrateur",
}
ROLE_ICONS = {"maitre_ouvrage": "🏛️", "chef_chantier": "👷", "ingenieur": "📐", "admin": "🛠️"}

# Droits par profil (principe du moindre privilège) -------------------------
#  - Maître d'ouvrage : pilote et suit (lecture + budget), ne saisit pas de données techniques.
#  - Chef de chantier : saisit le terrain (journal, dépenses, incidents), ne voit pas le budget global.
#  - Ingénieur        : contrôle et valide (essais, validation journal, clôture incidents, coûts).
#  - Administrateur   : gère comptes et affectations, lecture seule sur les données techniques.
PERMISSIONS = {
    "maitre_ouvrage": {
        "dashboard", "analyses", "budget_voir", "journal_lire", "couts_lire",
        "essais_lire", "incidents_lire", "chantier_creer", "chantier_affecter",
        "exporter", "messagerie",
    },
    "chef_chantier": {
        "dashboard", "analyses", "journal_lire", "journal_ecrire", "couts_ecrire",
        "essais_lire", "incidents_lire", "incidents_ecrire", "messagerie",
    },
    "ingenieur": {
        "dashboard", "analyses", "budget_voir", "journal_lire", "journal_valider",
        "couts_lire", "couts_ecrire", "essais_lire", "essais_ecrire",
        "incidents_lire", "incidents_ecrire", "incidents_cloturer", "exporter",
        "messagerie",
    },
    "admin": {
        "dashboard", "analyses", "budget_voir", "journal_lire", "couts_lire",
        "essais_lire", "incidents_lire", "chantier_creer", "chantier_affecter",
        "users_gerer", "exporter", "messagerie",
    },
}
PERMISSION_LABELS = {
    "dashboard": "Tableau de bord",
    "analyses": "Analyses multi-chantiers",
    "budget_voir": "Voir budgets et indicateurs financiers",
    "journal_lire": "Consulter le journal de chantier",
    "journal_ecrire": "Saisir le journal de chantier",
    "journal_valider": "Valider les journaux",
    "couts_lire": "Consulter toutes les dépenses",
    "couts_ecrire": "Saisir des dépenses",
    "essais_lire": "Consulter les essais",
    "essais_ecrire": "Saisir des essais de contrôle",
    "incidents_lire": "Consulter les incidents",
    "incidents_ecrire": "Déclarer des incidents",
    "incidents_cloturer": "Clôturer des incidents",
    "chantier_creer": "Créer des chantiers",
    "chantier_affecter": "Affecter des personnes aux chantiers",
    "users_gerer": "Gérer les comptes utilisateurs",
    "exporter": "Exporter les données (CSV)",
    "messagerie": "Messagerie",
}

METEO = ["Ensoleillé", "Nuageux", "Pluie faible", "Pluie forte", "Orage", "Vent fort"]
METEO_ARRET = {"Pluie forte", "Orage", "Vent fort"}
CATEGORIES_COUTS = ["Main d'œuvre", "Matériaux", "Matériel & engins", "Sous-traitance",
                    "Études & contrôles", "Divers"]
TYPES_INCIDENT = ["Sécurité – accident", "Sécurité – presqu'accident", "Non-conformité qualité",
                  "Environnement", "Retard / aléa", "Autre"]
GRAVITES = ["Faible", "Modérée", "Grave", "Critique"]
TYPES_OUVRAGE = ["Route", "Ouvrage d'art", "Bâtiment", "VRD / Réseaux", "Hydraulique", "Autre"]
STATUTS_CHANTIER = ["En cours", "Suspendu", "Terminé"]

# type d'essai -> (unité, seuil min par défaut, seuil max par défaut)
ESSAIS_TYPES = {
    "Résistance en compression du béton (28 j)": ("MPa", 25.0, None),
    "Affaissement au cône d'Abrams": ("mm", 50.0, 90.0),
    "Limite élastique de l'acier HA": ("MPa", 500.0, None),
    "Compactage du remblai (% OPM)": ("%", 95.0, None),
    "Module de plateforme EV2": ("MPa", 50.0, None),
    "Épaisseur de la couche d'enrobé": ("cm", 5.0, None),
    "Autre essai": ("", None, None),
}

MIN_PASSWORD = 8


# ----------------------------------------------------------------------------
# Base de données
# ----------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('maitre_ouvrage','chef_chantier','ingenieur','admin')),
    active INTEGER NOT NULL DEFAULT 1,
    must_change INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS chantiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT NOT NULL,
    localisation TEXT,
    type_ouvrage TEXT,
    budget_prevu REAL NOT NULL DEFAULT 0,
    date_debut TEXT NOT NULL,
    date_fin_prevue TEXT NOT NULL,
    statut TEXT NOT NULL DEFAULT 'En cours'
);
CREATE TABLE IF NOT EXISTS affectations (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chantier_id INTEGER NOT NULL REFERENCES chantiers(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, chantier_id)
);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chantier_id INTEGER NOT NULL REFERENCES chantiers(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    avancement_pct REAL NOT NULL,
    effectif INTEGER NOT NULL DEFAULT 0,
    heures REAL NOT NULL DEFAULT 0,
    meteo TEXT,
    temperature REAL,
    activites TEXT,
    observations TEXT,
    valide INTEGER NOT NULL DEFAULT 0,
    valide_par INTEGER REFERENCES users(id),
    auteur_id INTEGER NOT NULL REFERENCES users(id),
    UNIQUE (chantier_id, date)
);
CREATE TABLE IF NOT EXISTS couts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chantier_id INTEGER NOT NULL REFERENCES chantiers(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    categorie TEXT NOT NULL,
    description TEXT,
    montant REAL NOT NULL,
    auteur_id INTEGER NOT NULL REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS essais (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chantier_id INTEGER NOT NULL REFERENCES chantiers(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    type_essai TEXT NOT NULL,
    element TEXT,
    valeur REAL NOT NULL,
    unite TEXT,
    seuil_min REAL,
    seuil_max REAL,
    conforme INTEGER NOT NULL,
    auteur_id INTEGER NOT NULL REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chantier_id INTEGER NOT NULL REFERENCES chantiers(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    type TEXT NOT NULL,
    gravite TEXT NOT NULL,
    description TEXT,
    mesures TEXT,
    statut TEXT NOT NULL DEFAULT 'Ouvert',
    auteur_id INTEGER NOT NULL REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id INTEGER NOT NULL REFERENCES users(id),
    recipient_id INTEGER REFERENCES users(id),   -- NULL = message de canal de chantier
    chantier_id INTEGER REFERENCES chantiers(id) ON DELETE CASCADE,
    subject TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    lu INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_journal_ch ON journal(chantier_id, date);
CREATE INDEX IF NOT EXISTS idx_couts_ch ON couts(chantier_id, date);
CREATE INDEX IF NOT EXISTS idx_essais_ch ON essais(chantier_id, date);
CREATE INDEX IF NOT EXISTS idx_inc_ch ON incidents(chantier_id, date);
CREATE INDEX IF NOT EXISTS idx_msg_rec ON messages(recipient_id, lu);
"""

DATA_TABLES = {"journal", "couts", "essais", "incidents"}


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = get_conn()
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


def run(sql: str, params: tuple = ()) -> int:
    conn = get_conn()
    try:
        with conn:
            return conn.execute(sql, params).lastrowid
    finally:
        conn.close()


def run_many(statements: list[tuple[str, tuple]]) -> None:
    """Exécute plusieurs requêtes dans une seule transaction."""
    conn = get_conn()
    try:
        with conn:
            for sql, params in statements:
                conn.execute(sql, params)
    finally:
        conn.close()


def one(sql: str, params: tuple = ()) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def scalar(sql: str, params: tuple = ()):
    conn = get_conn()
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ----------------------------------------------------------------------------
# Mots de passe
# ----------------------------------------------------------------------------
def hash_pw(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"{salt.hex()}${digest.hex()}"


def check_pw(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), 200_000)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except ValueError:
        return False


# ----------------------------------------------------------------------------
# Initialisation et données de démonstration
# ----------------------------------------------------------------------------
def est_conforme(valeur: float, vmin, vmax) -> int:
    if vmin is not None and not pd.isna(vmin) and valeur < vmin:
        return 0
    if vmax is not None and not pd.isna(vmax) and valeur > vmax:
        return 0
    return 1


def seed_demo() -> None:
    rnd = random.Random(42)
    today = date.today()
    conn = get_conn()
    try:
        with conn:
            def add_user(username, pwd, name, role):
                return conn.execute(
                    "INSERT INTO users(username,password_hash,full_name,role,must_change) "
                    "VALUES (?,?,?,?,1)", (username, hash_pw(pwd), name, role)).lastrowid

            admin = add_user("admin", "Admin#2026", "Administrateur Système", "admin")
            moa = add_user("moa", "Moa#2026", "Direction des Travaux (MOA)", "maitre_ouvrage")
            chef1 = add_user("chef1", "Chef1#2026", "Chef de chantier 1", "chef_chantier")
            chef2 = add_user("chef2", "Chef2#2026", "Chef de chantier 2", "chef_chantier")
            ing1 = add_user("ing1", "Ing1#2026", "Ingénieur contrôle 1", "ingenieur")
            _ = admin

            # nom, localisation, type, budget, début, fin, vitesse, facteur coût, chef, essais
            chantiers = [
                ("Route régionale — Lot 1", "Tronçon de 12 km", "Route", 1_800_000_000,
                 today - timedelta(days=120), today + timedelta(days=180), 0.93, 1.06, chef1,
                 [("Compactage du remblai (% OPM)", 96.0, 1.8),
                  ("Module de plateforme EV2", 58.0, 6.0),
                  ("Épaisseur de la couche d'enrobé", 5.6, 0.4)]),
                ("Pont de franchissement", "Franchissement de cours d'eau", "Ouvrage d'art",
                 950_000_000, today - timedelta(days=90), today + timedelta(days=150),
                 0.78, 1.18, chef2,
                 [("Résistance en compression du béton (28 j)", 27.5, 3.4),
                  ("Limite élastique de l'acier HA", 530.0, 25.0),
                  ("Affaissement au cône d'Abrams", 75.0, 14.0)]),
                ("Bâtiment administratif R+4", "Zone administrative", "Bâtiment",
                 1_200_000_000, today - timedelta(days=60), today + timedelta(days=240),
                 1.02, 0.97, chef1,
                 [("Résistance en compression du béton (28 j)", 30.0, 3.0),
                  ("Affaissement au cône d'Abrams", 70.0, 10.0)]),
            ]
            shares = [0.28, 0.38, 0.20, 0.08, 0.03, 0.03]
            activites = {
                "Route": ["Terrassements généraux", "Mise en œuvre de la couche de forme",
                          "Pose de buses", "Réglage et compactage"],
                "Ouvrage d'art": ["Ferraillage des piles", "Coulage du béton des fondations",
                                  "Coffrage du tablier", "Battage de pieux"],
                "Bâtiment": ["Fondations et longrines", "Élévation des poteaux",
                             "Coulage du plancher", "Maçonnerie de remplissage"],
            }
            ids = []
            for (nom, loc, typ, budget, debut, fin, speed, costf, chef, essais) in chantiers:
                cid = conn.execute(
                    "INSERT INTO chantiers(nom,localisation,type_ouvrage,budget_prevu,date_debut,"
                    "date_fin_prevue,statut) VALUES (?,?,?,?,?,?,'En cours')",
                    (nom, loc, typ, budget, debut.isoformat(), fin.isoformat())).lastrowid
                ids.append(cid)
                for uid in {moa, ing1, chef}:
                    conn.execute("INSERT OR IGNORE INTO affectations VALUES (?,?)", (uid, cid))

                total = (fin - debut).days
                last, last_cost, d, i = 0.0, 0.0, debut, 0
                while d <= today:
                    if d.weekday() != 6:  # pas de saisie le dimanche
                        planned = (d - debut).days / total * 100
                        last = max(last, min(100.0, planned * speed + rnd.uniform(-0.8, 0.8)))
                        effectif = rnd.randint(25, 60)
                        meteo = rnd.choices(METEO, weights=[40, 30, 15, 7, 4, 4])[0]
                        conn.execute(
                            "INSERT INTO journal(chantier_id,date,avancement_pct,effectif,heures,"
                            "meteo,temperature,activites,observations,valide,valide_par,auteur_id) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (cid, d.isoformat(), round(last, 2), effectif,
                             round(effectif * 8 * rnd.uniform(0.85, 1.0), 1), meteo,
                             round(rnd.uniform(23, 33), 1), rnd.choice(activites[typ]), "",
                             1 if d < today - timedelta(days=3) else 0,
                             ing1 if d < today - timedelta(days=3) else None, chef))
                    if i > 0 and i % 7 == 0:
                        delta = last - last_cost
                        last_cost = last
                        total_cout = delta / 100 * budget * costf * rnd.uniform(0.95, 1.05)
                        for cat, sh in zip(CATEGORIES_COUTS, shares):
                            conn.execute(
                                "INSERT INTO couts(chantier_id,date,categorie,description,montant,"
                                "auteur_id) VALUES (?,?,?,?,?,?)",
                                (cid, d.isoformat(), cat, "Situation hebdomadaire",
                                 round(total_cout * sh, 0), chef))
                    if i > 0 and i % 9 == 0:
                        t, mu, sd = rnd.choice(essais)
                        val = round(rnd.gauss(mu, sd), 2)
                        unite, vmin, vmax = ESSAIS_TYPES[t]
                        conn.execute(
                            "INSERT INTO essais(chantier_id,date,type_essai,element,valeur,unite,"
                            "seuil_min,seuil_max,conforme,auteur_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (cid, d.isoformat(), t, f"Prélèvement n°{i // 9}", val, unite, vmin,
                             vmax, est_conforme(val, vmin, vmax), ing1))
                    d += timedelta(days=1)
                    i += 1

                for _k in range(rnd.randint(3, 5)):
                    jour = debut + timedelta(days=rnd.randint(5, max((today - debut).days, 6)))
                    if jour > today:
                        jour = today
                    conn.execute(
                        "INSERT INTO incidents(chantier_id,date,type,gravite,description,mesures,"
                        "statut,auteur_id) VALUES (?,?,?,?,?,?,?,?)",
                        (cid, jour.isoformat(), rnd.choice(TYPES_INCIDENT),
                         rnd.choice(GRAVITES[:3]), "Événement relevé lors de la ronde.",
                         "Mesures correctives mises en place.", "Clôturé", chef))
            conn.execute(
                "INSERT INTO incidents(chantier_id,date,type,gravite,description,mesures,statut,"
                "auteur_id) VALUES (?,?,?,?,?,?,?,?)",
                (ids[1], today.isoformat(), "Sécurité – accident", "Grave",
                 "Chute d'un opérateur depuis l'échafaudage du tablier.",
                 "Zone balisée, enquête en cours.", "Ouvert", chef2))
            conn.execute(
                "INSERT INTO messages(sender_id,recipient_id,chantier_id,subject,body,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (moa, ing1, ids[1], "Retard sur le pont",
                 "Merci de me transmettre un plan de rattrapage pour le pont avant vendredi.",
                 now_iso()))
            conn.execute(
                "INSERT INTO messages(sender_id,recipient_id,chantier_id,subject,body,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (chef2, None, ids[1], None,
                 "Livraison d'acier prévue demain matin, prévoir l'accès au chantier.", now_iso()))
    finally:
        conn.close()


@st.cache_resource
def bootstrap() -> bool:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
    if scalar("SELECT COUNT(*) FROM users") == 0:
        if DEMO:
            seed_demo()
        else:
            pwd = os.environ.get("SCREENLEAD_ADMIN_PASSWORD") or secrets.token_urlsafe(10)
            run("INSERT INTO users(username,password_hash,full_name,role,must_change) "
                "VALUES ('admin',?,?, 'admin',1)", (hash_pw(pwd), "Administrateur"))
            print(f"[Screenlead] Compte initial : admin / {pwd}  (à changer à la 1re connexion)")
    return True


# ----------------------------------------------------------------------------
# Authentification et droits
# ----------------------------------------------------------------------------
def authenticate(username: str, password: str) -> dict | None:
    row = one("SELECT * FROM users WHERE username=? AND active=1", (username.strip(),))
    if row is None:
        hash_pw(password)  # temps de réponse constant
        return None
    return row if check_pw(password, row["password_hash"]) else None


def refresh_user() -> dict | None:
    u = st.session_state.get("user")
    if not u:
        return None
    row = one("SELECT id, username, full_name, role, active, must_change FROM users WHERE id=?",
              (u["id"],))
    if not row or not row["active"]:
        st.session_state.clear()
        return None
    st.session_state["user"] = row
    return row


def can(permission: str) -> bool:
    u = st.session_state.get("user")
    return bool(u) and permission in PERMISSIONS.get(u["role"], set())


def chantiers_visibles(user: dict) -> pd.DataFrame:
    """Un utilisateur ne voit que les chantiers auxquels il est affecté (admin : tous)."""
    if user["role"] == "admin":
        return q("SELECT * FROM chantiers ORDER BY nom")
    return q("SELECT c.* FROM chantiers c JOIN affectations a ON a.chantier_id=c.id "
             "WHERE a.user_id=? ORDER BY c.nom", (int(user["id"]),))


def login_page() -> None:
    _, col, _ = st.columns([1, 1.2, 1])
    with col:
        st.title("🏗️ Screenlead")
        st.caption("Collecte, analyse et gestion des données de chantiers")
        locked = st.session_state.get("locked_until", 0) - time.time()
        with st.form("login"):
            username = st.text_input("Identifiant")
            password = st.text_input("Mot de passe", type="password")
            ok = st.form_submit_button("Se connecter")
        if ok:
            if locked > 0:
                st.error(f"Trop de tentatives. Réessayez dans {int(locked)} s.")
            else:
                user = authenticate(username, password)
                if user:
                    st.session_state["user"] = {k: user[k] for k in
                                                ("id", "username", "full_name", "role", "active",
                                                 "must_change")}
                    st.session_state["fails"] = 0
                    st.rerun()
                else:
                    st.session_state["fails"] = st.session_state.get("fails", 0) + 1
                    if st.session_state["fails"] >= 5:
                        st.session_state["locked_until"] = time.time() + 60
                        st.session_state["fails"] = 0
                    st.error("Identifiant ou mot de passe incorrect.")
        if DEMO:
            with st.expander("Comptes de démonstration"):
                st.markdown(
                    "| Profil | Identifiant | Mot de passe |\n|---|---|---|\n"
                    "| Maître d'ouvrage | `moa` | `Moa#2026` |\n"
                    "| Chef de chantier | `chef1` / `chef2` | `Chef1#2026` / `Chef2#2026` |\n"
                    "| Ingénieur | `ing1` | `Ing1#2026` |\n"
                    "| Administrateur | `admin` | `Admin#2026` |")


# ----------------------------------------------------------------------------
# Utilitaires d'affichage et de calcul
# ----------------------------------------------------------------------------
def money(v) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{v:,.0f}".replace(",", " ") + f" {DEVISE}"


def show_df(df: pd.DataFrame) -> None:
    try:
        st.dataframe(df, hide_index=True, width="stretch")
    except Exception:
        st.dataframe(df, hide_index=True, use_container_width=True)


def show_fig(fig) -> None:
    try:
        st.plotly_chart(fig, width="stretch")
    except Exception:
        st.plotly_chart(fig, use_container_width=True)


def names_map(ch: pd.DataFrame) -> dict[int, str]:
    return {int(i): n for i, n in zip(ch["id"], ch["nom"])}


def ids_of(ch: pd.DataFrame) -> list[int]:
    return [int(i) for i in ch["id"]]


def pick_chantiers(ch: pd.DataFrame, key: str) -> list[int]:
    names = names_map(ch)
    return st.multiselect("Chantiers", options=list(names), default=list(names),
                          format_func=names.get, key=key)


def fetch(table: str, ids: list[int], d1: date | None = None, d2: date | None = None) -> pd.DataFrame:
    assert table in DATA_TABLES
    if not ids:
        return pd.DataFrame()
    ph = ",".join("?" * len(ids))
    sql = (f"SELECT t.*, c.nom AS chantier, u.full_name AS auteur FROM {table} t "
           f"JOIN chantiers c ON c.id=t.chantier_id JOIN users u ON u.id=t.auteur_id "
           f"WHERE t.chantier_id IN ({ph})")
    params: list = list(ids)
    if d1:
        sql += " AND t.date >= ?"
        params.append(d1.isoformat())
    if d2:
        sql += " AND t.date <= ?"
        params.append(d2.isoformat())
    sql += " ORDER BY t.date DESC, t.id DESC"
    return q(sql, tuple(params))


def guard_access(cid: int, ch: pd.DataFrame) -> None:
    if int(cid) not in ids_of(ch):
        st.error("Accès refusé à ce chantier.")
        st.stop()


def compute_kpis(ids: list[int]) -> pd.DataFrame:
    """Indicateurs par chantier : avancement, SPI/CPI (méthode de la valeur acquise), qualité, sécurité."""
    if not ids:
        return pd.DataFrame()
    ph = ",".join("?" * len(ids))
    chs = q(f"SELECT * FROM chantiers WHERE id IN ({ph}) ORDER BY nom", tuple(ids))
    today = date.today()
    nan = float("nan")
    rows = []
    for c in chs.to_dict("records"):
        cid = int(c["id"])
        last = scalar("SELECT avancement_pct FROM journal WHERE chantier_id=? "
                      "ORDER BY date DESC LIMIT 1", (cid,))
        reel = float(last) if last is not None else 0.0
        debut = date.fromisoformat(c["date_debut"])
        fin = date.fromisoformat(c["date_fin_prevue"])
        total = max((fin - debut).days, 1)
        ecoule = min(max((today - debut).days, 0), total)
        prevu = ecoule / total * 100
        restants = max((fin - today).days, 0)
        budget = float(c["budget_prevu"] or 0)
        depense = float(scalar("SELECT COALESCE(SUM(montant),0) FROM couts WHERE chantier_id=?",
                               (cid,)) or 0)
        valeur_acquise = reel / 100 * budget
        cpi = valeur_acquise / depense if depense > 0 else nan
        n_ess = scalar("SELECT COUNT(*) FROM essais WHERE chantier_id=?", (cid,)) or 0
        ok_ess = scalar("SELECT COALESCE(SUM(conforme),0) FROM essais WHERE chantier_id=?",
                        (cid,)) or 0
        limite = (today - timedelta(days=30)).isoformat()
        rows.append({
            "id": cid, "nom": c["nom"], "type": c["type_ouvrage"], "statut": c["statut"],
            "budget": budget, "depense": depense, "valeur_acquise": valeur_acquise,
            "reel": reel, "prevu": prevu,
            "spi": reel / prevu if prevu > 0 else nan,
            "cpi": cpi,
            "eac": budget / cpi if cpi and not pd.isna(cpi) and cpi > 0 else nan,
            "jours_restants": restants,
            "retard_jours": max(prevu - reel, 0) / 100 * total,
            "v_moy": reel / ecoule if ecoule > 0 else nan,
            "v_req": (100 - reel) / restants if restants > 0 else nan,
            "conformite": ok_ess / n_ess * 100 if n_ess else nan,
            "nc_30j": scalar("SELECT COUNT(*) FROM essais WHERE chantier_id=? AND conforme=0 "
                             "AND date>=?", (cid, limite)) or 0,
            "inc_ouverts": scalar("SELECT COUNT(*) FROM incidents WHERE chantier_id=? "
                                  "AND statut='Ouvert'", (cid,)) or 0,
            "inc_graves": scalar("SELECT COUNT(*) FROM incidents WHERE chantier_id=? "
                                 "AND statut='Ouvert' AND gravite IN ('Grave','Critique')",
                                 (cid,)) or 0,
        })
    return pd.DataFrame(rows)


def alertes(k: pd.DataFrame, avec_budget: bool) -> list[tuple[str, str]]:
    out = []
    for r in k.itertuples():
        if r.inc_graves > 0:
            out.append(("error", f"**{r.nom}** — {r.inc_graves} incident(s) grave(s)/critique(s) ouvert(s)."))
        if r.spi < 0.90:
            out.append(("warning", f"**{r.nom}** — retard : {r.reel:.1f} % réalisé pour {r.prevu:.1f} % prévu "
                                   f"(SPI {r.spi:.2f}, ≈ {r.retard_jours:.0f} j de retard)."))
        if avec_budget and r.cpi < 0.90:
            out.append(("warning", f"**{r.nom}** — dérive des coûts (CPI {r.cpi:.2f}) : "
                                   f"coût final estimé {money(r.eac)} pour un budget de {money(r.budget)}."))
        if r.nc_30j > 0:
            out.append(("warning", f"**{r.nom}** — {r.nc_30j} essai(s) non conforme(s) ces 30 derniers jours."))
    return out


# ----------------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------------
def page_dashboard(user: dict, ch: pd.DataFrame) -> None:
    st.header("📊 Tableau de bord")
    if ch.empty:
        st.info("Aucun chantier ne vous est affecté pour le moment.")
        return
    avec_budget = can("budget_voir")
    ids = pick_chantiers(ch, "dash_ch")
    k = compute_kpis(ids)
    if k.empty:
        st.info("Sélectionnez au moins un chantier.")
        return

    cols = st.columns(4 if avec_budget else 3)
    cols[0].metric("Chantiers", len(k))
    cols[1].metric("Avancement moyen", f"{k['reel'].mean():.1f} %",
                   f"{k['reel'].mean() - k['prevu'].mean():+.1f} pts vs prévu")
    cols[2].metric("Incidents ouverts", int(k["inc_ouverts"].sum()))
    if avec_budget:
        cols[3].metric("Dépenses / budget", f"{k['depense'].sum() / max(k['budget'].sum(), 1) * 100:.1f} %",
                       money(k["depense"].sum()), delta_color="off")

    for level, msg in alertes(k, avec_budget):
        (st.error if level == "error" else st.warning)(msg)

    fig = go.Figure()
    fig.add_bar(x=k["nom"], y=k["reel"].round(1), name="Avancement réel (%)")
    fig.add_bar(x=k["nom"], y=k["prevu"].round(1), name="Avancement prévu (%)")
    fig.update_layout(barmode="group", yaxis_title="%", height=350, margin=dict(t=30))
    show_fig(fig)

    d = pd.DataFrame({
        "Chantier": k["nom"], "Type": k["type"], "Statut": k["statut"],
        "Réel (%)": k["reel"].round(1), "Prévu (%)": k["prevu"].round(1),
        "SPI": k["spi"].round(2), "Jours restants": k["jours_restants"],
        "Conformité essais (%)": k["conformite"].round(1),
        "Incidents ouverts": k["inc_ouverts"],
    })
    if avec_budget:
        d["Budget"] = k["budget"].map(money)
        d["Dépensé"] = k["depense"].map(money)
        d["CPI"] = k["cpi"].round(2)
    show_df(d)
    st.caption("SPI = avancement réel / prévu (< 1 : retard). CPI = valeur acquise / coût réel (< 1 : dépassement).")


def page_journal(user: dict, ch: pd.DataFrame) -> None:
    st.header("📓 Journal de chantier")
    ids, names = ids_of(ch), names_map(ch)
    labels = []
    if can("journal_ecrire"):
        labels.append("Saisir")
    labels.append("Consulter")
    if can("journal_valider"):
        labels.append("Valider")
    tabs = dict(zip(labels, st.tabs(labels)))

    if "Saisir" in tabs:
        with tabs["Saisir"]:
            if not ids:
                st.info("Aucun chantier affecté.")
            else:
                with st.form("form_journal", clear_on_submit=True):
                    cid = st.selectbox("Chantier", ids, format_func=names.get)
                    c1, c2, c3 = st.columns(3)
                    d = c1.date_input("Date", value=date.today(), max_value=date.today())
                    avance = c2.number_input("Avancement cumulé (%)", 0.0, 100.0, step=0.5)
                    effectif = c3.number_input("Effectif présent", 0, 5000, step=1)
                    c4, c5, c6 = st.columns(3)
                    heures = c4.number_input("Heures travaillées (total)", 0.0, 100000.0, step=8.0)
                    meteo = c5.selectbox("Météo", METEO)
                    temp = c6.number_input("Température (°C)", -10.0, 60.0, 28.0, step=0.5)
                    activites = st.text_area("Activités réalisées")
                    obs = st.text_area("Observations / difficultés")
                    ok = st.form_submit_button("Enregistrer")
                if ok:
                    guard_access(cid, ch)
                    precedent = scalar("SELECT MAX(avancement_pct) FROM journal WHERE chantier_id=? "
                                       "AND date<?", (cid, d.isoformat()))
                    if precedent is not None and avance < precedent:
                        st.error(f"L'avancement cumulé ne peut pas être inférieur à {precedent:.1f} % "
                                 "(valeur précédente).")
                    elif not activites.strip():
                        st.error("Décrivez les activités réalisées.")
                    else:
                        try:
                            run("INSERT INTO journal(chantier_id,date,avancement_pct,effectif,heures,"
                                "meteo,temperature,activites,observations,auteur_id) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (cid, d.isoformat(), avance, int(effectif), heures, meteo, temp,
                                 activites.strip(), obs.strip(), int(user["id"])))
                            st.success("Journal enregistré.")
                        except sqlite3.IntegrityError:
                            st.error("Une saisie existe déjà pour ce chantier à cette date.")

    with tabs["Consulter"]:
        sel = pick_chantiers(ch, "journal_ch")
        df = fetch("journal", sel)
        if df.empty:
            st.info("Aucune saisie.")
        else:
            df["valide"] = df["valide"].map({1: "✅ Validé", 0: "⏳ En attente"})
            show_df(df[["date", "chantier", "avancement_pct", "effectif", "heures", "meteo",
                        "temperature", "activites", "observations", "valide", "auteur"]]
                    .rename(columns={"avancement_pct": "avancement (%)", "temperature": "temp. (°C)"}))

    if "Valider" in tabs:
        with tabs["Valider"]:
            df = fetch("journal", ids)
            att = df[df["valide"] == 0] if not df.empty else df
            if att.empty:
                st.success("Tous les journaux sont validés.")
            else:
                options = {int(r.id): f"{r.date} — {r.chantier} — {r.avancement_pct:.1f} %" for r in att.itertuples()}
                choix = st.multiselect("Journaux à valider", list(options), format_func=options.get)
                if st.button("Valider la sélection") and choix:
                    run_many([("UPDATE journal SET valide=1, valide_par=? WHERE id=? AND chantier_id IN "
                               f"({','.join('?' * len(ids))})", (int(user['id']), int(i), *ids))
                              for i in choix])
                    st.rerun()


def page_couts(user: dict, ch: pd.DataFrame) -> None:
    st.header("💰 Dépenses")
    ids, names = ids_of(ch), names_map(ch)
    if can("couts_ecrire") and ids:
        with st.expander("➕ Saisir une dépense", expanded=not can("couts_lire")):
            with st.form("form_cout", clear_on_submit=True):
                cid = st.selectbox("Chantier", ids, format_func=names.get)
                c1, c2, c3 = st.columns(3)
                d = c1.date_input("Date", value=date.today(), max_value=date.today())
                cat = c2.selectbox("Catégorie", CATEGORIES_COUTS)
                montant = c3.number_input(f"Montant ({DEVISE})", 0.0, step=10000.0)
                desc = st.text_input("Description / référence (facture, bon, situation)")
                ok = st.form_submit_button("Enregistrer")
            if ok:
                guard_access(cid, ch)
                if montant <= 0:
                    st.error("Le montant doit être supérieur à 0.")
                else:
                    run("INSERT INTO couts(chantier_id,date,categorie,description,montant,auteur_id) "
                        "VALUES (?,?,?,?,?,?)", (cid, d.isoformat(), cat, desc.strip(), montant, int(user["id"])))
                    st.success("Dépense enregistrée.")

    sel = pick_chantiers(ch, "couts_ch")
    df = fetch("couts", sel)
    if not can("couts_lire"):
        st.caption("Vous ne voyez que vos propres saisies.")
        df = df[df["auteur_id"] == int(user["id"])] if not df.empty else df
    if df.empty:
        st.info("Aucune dépense.")
        return
    st.metric("Total affiché", money(df["montant"].sum()))
    out = df[["date", "chantier", "categorie", "description", "montant", "auteur"]].copy()
    out["montant"] = out["montant"].map(money)
    show_df(out)


def page_essais(user: dict, ch: pd.DataFrame) -> None:
    st.header("🧪 Essais & contrôles qualité")
    ids, names = ids_of(ch), names_map(ch)
    if can("essais_ecrire") and ids:
        with st.expander("➕ Saisir un essai"):
            type_essai = st.selectbox("Type d'essai", list(ESSAIS_TYPES), key="essai_type")
            unite, pmin, pmax = ESSAIS_TYPES[type_essai]
            with st.form("form_essai", clear_on_submit=True):
                c1, c2 = st.columns(2)
                cid = c1.selectbox("Chantier", ids, format_func=names.get)
                d = c2.date_input("Date", value=date.today(), max_value=date.today())
                element = st.text_input("Élément / zone testé(e) (ex. poutre P3, PK 2+300)")
                c3, c4 = st.columns(2)
                valeur = c3.number_input("Valeur mesurée", value=0.0, format="%.2f")
                unite_in = c4.text_input("Unité", value=unite, key=f"u_{type_essai}")
                c5, c6 = st.columns(2)
                use_min = c5.checkbox("Seuil minimal", value=pmin is not None, key=f"cmin_{type_essai}")
                smin = c5.number_input("Valeur du seuil min", value=float(pmin) if pmin is not None else 0.0,
                                       key=f"vmin_{type_essai}")
                use_max = c6.checkbox("Seuil maximal", value=pmax is not None, key=f"cmax_{type_essai}")
                smax = c6.number_input("Valeur du seuil max", value=float(pmax) if pmax is not None else 0.0,
                                       key=f"vmax_{type_essai}")
                ok = st.form_submit_button("Enregistrer")
            if ok:
                guard_access(cid, ch)
                vmin = smin if use_min else None
                vmax = smax if use_max else None
                conf = est_conforme(valeur, vmin, vmax)
                run("INSERT INTO essais(chantier_id,date,type_essai,element,valeur,unite,seuil_min,"
                    "seuil_max,conforme,auteur_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (cid, d.isoformat(), type_essai, element.strip(), valeur, unite_in.strip(),
                     vmin, vmax, conf, int(user["id"])))
                if conf:
                    st.success("Essai enregistré : conforme.")
                else:
                    run("INSERT INTO incidents(chantier_id,date,type,gravite,description,mesures,"
                        "statut,auteur_id) VALUES (?,?,?,?,?,?,?,?)",
                        (cid, d.isoformat(), "Non-conformité qualité", "Modérée",
                         f"{type_essai} : {valeur} {unite_in} sur « {element.strip() or 'n/d'} » hors seuils.",
                         "", "Ouvert", int(user["id"])))
                    st.warning("Essai enregistré : NON CONFORME. Une non-conformité a été ouverte automatiquement.")

    sel = pick_chantiers(ch, "essais_ch")
    df = fetch("essais", sel)
    if df.empty:
        st.info("Aucun essai.")
        return
    df["résultat"] = df["conforme"].map({1: "✅ Conforme", 0: "❌ Non conforme"})
    show_df(df[["date", "chantier", "type_essai", "element", "valeur", "unite", "seuil_min",
                "seuil_max", "résultat", "auteur"]])


def page_incidents(user: dict, ch: pd.DataFrame) -> None:
    st.header("⚠️ Incidents & sécurité")
    ids, names = ids_of(ch), names_map(ch)
    if can("incidents_ecrire") and ids:
        with st.expander("➕ Déclarer un incident"):
            with st.form("form_inc", clear_on_submit=True):
                c1, c2 = st.columns(2)
                cid = c1.selectbox("Chantier", ids, format_func=names.get)
                d = c2.date_input("Date", value=date.today(), max_value=date.today())
                c3, c4 = st.columns(2)
                typ = c3.selectbox("Type", TYPES_INCIDENT)
                grav = c4.selectbox("Gravité", GRAVITES)
                desc = st.text_area("Description des faits")
                mesures = st.text_area("Mesures immédiates / correctives")
                ok = st.form_submit_button("Déclarer")
            if ok:
                guard_access(cid, ch)
                if not desc.strip():
                    st.error("La description est obligatoire.")
                else:
                    run("INSERT INTO incidents(chantier_id,date,type,gravite,description,mesures,"
                        "statut,auteur_id) VALUES (?,?,?,?,?,?,'Ouvert',?)",
                        (cid, d.isoformat(), typ, grav, desc.strip(), mesures.strip(), int(user["id"])))
                    st.success("Incident déclaré.")

    sel = pick_chantiers(ch, "inc_ch")
    df = fetch("incidents", sel)
    if df.empty:
        st.info("Aucun incident.")
        return
    statut = st.radio("Statut", ["Tous", "Ouvert", "Clôturé"], horizontal=True)
    vue = df if statut == "Tous" else df[df["statut"] == statut]
    show_df(vue[["id", "date", "chantier", "type", "gravite", "description", "mesures", "statut", "auteur"]])

    if can("incidents_cloturer"):
        ouverts = df[df["statut"] == "Ouvert"]
        if not ouverts.empty:
            opts = {int(r.id): f"#{r.id} — {r.date} — {r.chantier} — {r.type} ({r.gravite})" for r in ouverts.itertuples()}
            choix = st.multiselect("Clôturer des incidents", list(opts), format_func=opts.get)
            if st.button("Clôturer la sélection") and choix:
                run_many([(f"UPDATE incidents SET statut='Clôturé' WHERE id=? AND chantier_id IN "
                           f"({','.join('?' * len(ids))})", (int(i), *ids)) for i in choix])
                st.rerun()


def page_analyses(user: dict, ch: pd.DataFrame) -> None:
    st.header("📈 Analyses multi-chantiers")
    if ch.empty:
        st.info("Aucun chantier affecté.")
        return
    avec_budget = can("budget_voir")
    sel = pick_chantiers(ch, "an_ch")
    if not sel:
        st.info("Sélectionnez au moins un chantier.")
        return
    dmin = min(date.fromisoformat(x) for x in ch["date_debut"])
    dmin = min(dmin, date.today())
    periode = st.date_input("Période d'analyse", value=(dmin, date.today()), key="an_periode")
    if not isinstance(periode, tuple) or len(periode) != 2:
        st.info("Sélectionnez une date de début et une date de fin.")
        return
    d1, d2 = periode

    k = compute_kpis(sel)
    data = {t: fetch(t, sel, d1, d2) for t in ("journal", "couts", "essais", "incidents")}
    for df in data.values():
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])

    labels = ["Avancement"] + (["Coûts"] if avec_budget else []) + ["Qualité", "Sécurité",
                                                                    "Ressources & météo", "Comparaison"]
    tabs = dict(zip(labels, st.tabs(labels)))

    # --- Avancement ----------------------------------------------------------
    with tabs["Avancement"]:
        j = data["journal"]
        if j.empty:
            st.info("Pas de données de journal sur la période.")
        else:
            fig = go.Figure()
            for r in k.itertuples():
                jj = j[j["chantier"] == r.nom].sort_values("date")
                fig.add_scatter(x=jj["date"], y=jj["avancement_pct"], mode="lines", name=f"{r.nom} — réel")
                c = ch[ch["id"] == r.id].iloc[0]
                fig.add_scatter(x=[c["date_debut"], c["date_fin_prevue"]], y=[0, 100], mode="lines",
                                line=dict(dash="dash"), name=f"{r.nom} — prévu")
            fig.update_layout(yaxis_title="Avancement cumulé (%)", height=420, margin=dict(t=30))
            show_fig(fig)
        v = pd.DataFrame({
            "Chantier": k["nom"], "Réel (%)": k["reel"].round(1), "Prévu (%)": k["prevu"].round(1),
            "SPI": k["spi"].round(2), "Retard estimé (j)": k["retard_jours"].round(0),
            "Vitesse moyenne (%/j)": k["v_moy"].round(3),
            "Vitesse requise (%/j)": k["v_req"].round(3), "Jours restants": k["jours_restants"],
        })
        show_df(v)
        st.caption("Si la vitesse requise dépasse nettement la vitesse moyenne, la date de fin prévue est compromise.")

    # --- Coûts ---------------------------------------------------------------
    if "Coûts" in tabs:
        with tabs["Coûts"]:
            c = data["couts"]
            if c.empty:
                st.info("Pas de dépenses sur la période.")
            else:
                cum = c.groupby(["chantier", "date"])["montant"].sum().groupby(level=0).cumsum().reset_index()
                show_fig(px.line(cum, x="date", y="montant", color="chantier",
                                 labels={"montant": f"Dépenses cumulées ({DEVISE})"}, title="Dépenses cumulées"))
                par_cat = c.groupby(["categorie", "chantier"])["montant"].sum().reset_index()
                show_fig(px.bar(par_cat, x="categorie", y="montant", color="chantier",
                                title="Répartition par catégorie"))
            f = pd.DataFrame({
                "Chantier": k["nom"], "Budget": k["budget"].map(money), "Dépensé": k["depense"].map(money),
                "Consommé (%)": (k["depense"] / k["budget"].where(k["budget"] > 0) * 100).round(1),
                "Avancement (%)": k["reel"].round(1), "CPI": k["cpi"].round(2),
                "Coût final estimé": k["eac"].map(money),
                "Écart prévisionnel": (k["eac"] - k["budget"]).map(money),
            })
            show_df(f)
            st.caption("Coût final estimé = budget / CPI (méthode de la valeur acquise).")

    # --- Qualité -------------------------------------------------------------
    with tabs["Qualité"]:
        e = data["essais"]
        if e.empty:
            st.info("Aucun essai sur la période.")
        else:
            g = e.groupby("type_essai").agg(essais=("id", "count"), conformes=("conforme", "sum")).reset_index()
            g["taux de conformité (%)"] = (g["conformes"] / g["essais"] * 100).round(1)
            show_fig(px.bar(g, x="type_essai", y="taux de conformité (%)", range_y=[0, 100],
                            title="Taux de conformité par type d'essai"))
            show_df(g)
            t = st.selectbox("Détail d'un type d'essai", sorted(e["type_essai"].unique()))
            ee = e[e["type_essai"] == t].copy()
            ee["résultat"] = ee["conforme"].map({1: "Conforme", 0: "Non conforme"})
            fig = px.scatter(ee, x="date", y="valeur", color="chantier", symbol="résultat",
                             hover_data=["element"], title=t)
            if ee["seuil_min"].notna().any():
                fig.add_hline(y=float(ee["seuil_min"].dropna().iloc[0]), line_dash="dash",
                              annotation_text="seuil min")
            if ee["seuil_max"].notna().any():
                fig.add_hline(y=float(ee["seuil_max"].dropna().iloc[0]), line_dash="dash",
                              annotation_text="seuil max")
            show_fig(fig)

    # --- Sécurité ------------------------------------------------------------
    with tabs["Sécurité"]:
        inc, j = data["incidents"], data["journal"]
        if inc.empty:
            st.info("Aucun incident sur la période.")
        else:
            cnt = inc.groupby(["type", "gravite"]).size().reset_index(name="nombre")
            show_fig(px.bar(cnt, x="type", y="nombre", color="gravite", title="Incidents par type et gravité"))
        if not j.empty:
            h = j.groupby("chantier")["heures"].sum()
            acc = (inc[inc["type"] == "Sécurité – accident"].groupby("chantier").size()
                   if not inc.empty else pd.Series(dtype=float))
            tf = pd.DataFrame({"heures travaillées": h, "accidents": acc}).fillna(0)
            tf["taux de fréquence"] = (tf["accidents"] * 1e6 / tf["heures travaillées"].where(tf["heures travaillées"] > 0)).round(1)
            show_df(tf.reset_index().rename(columns={"index": "chantier"}))
            st.caption("Taux de fréquence = accidents × 1 000 000 / heures travaillées.")

    # --- Ressources & météo --------------------------------------------------
    with tabs["Ressources & météo"]:
        j = data["journal"]
        if j.empty:
            st.info("Pas de données de journal sur la période.")
        else:
            j = j.sort_values(["chantier", "date"]).copy()
            j["gain_jour"] = j.groupby("chantier")["avancement_pct"].diff()
            res = j.groupby("chantier").agg(
                effectif_moyen=("effectif", "mean"), heures_totales=("heures", "sum"),
                jours_saisis=("id", "count"), avancement_final=("avancement_pct", "max"),
                jours_arret_meteo=("meteo", lambda s: int(s.isin(METEO_ARRET).sum())),
            ).reset_index()
            res["avancement par 1000 h (%)"] = (res["avancement_final"] / res["heures_totales"].where(res["heures_totales"] > 0) * 1000).round(2)
            res["effectif_moyen"] = res["effectif_moyen"].round(1)
            show_df(res)
            m = j.dropna(subset=["gain_jour"]).groupby("meteo")["gain_jour"].mean().reset_index()
            if not m.empty:
                show_fig(px.bar(m, x="meteo", y="gain_jour", title="Gain d'avancement moyen par jour selon la météo",
                                labels={"gain_jour": "points d'avancement / jour"}))

    # --- Comparaison ---------------------------------------------------------
    with tabs["Comparaison"]:
        cols = {"nom": "Chantier", "reel": "Réel (%)", "prevu": "Prévu (%)", "spi": "SPI",
                "conformite": "Conformité (%)", "inc_ouverts": "Incidents ouverts"}
        if avec_budget:
            cols["cpi"] = "CPI"
        comp = k[list(cols)].rename(columns=cols).round(2)
        show_df(comp)
        idx = ["SPI"] + (["CPI"] if avec_budget else [])
        long = comp.melt(id_vars="Chantier", value_vars=idx, var_name="Indicateur", value_name="Valeur")
        fig = px.bar(long, x="Chantier", y="Valeur", color="Indicateur", barmode="group",
                     title="Performance délais / coûts (1 = conforme au plan)")
        fig.add_hline(y=1, line_dash="dash")
        show_fig(fig)

    if can("exporter"):
        with st.expander("⬇️ Exporter les données de la période (CSV)"):
            for name, df in data.items():
                if not df.empty:
                    st.download_button(f"Télécharger {name}.csv", df.to_csv(index=False).encode("utf-8-sig"),
                                       file_name=f"screenlead_{name}.csv", mime="text/csv", key=f"dl_{name}")


# --- Messagerie -------------------------------------------------------------
def unread_count(uid: int) -> int:
    return int(scalar("SELECT COUNT(*) FROM messages WHERE recipient_id=? AND lu=0", (uid,)) or 0)


def send_direct(sender: int, recipient: int, subject: str, body: str, chantier_id: int | None = None) -> None:
    run("INSERT INTO messages(sender_id,recipient_id,chantier_id,subject,body,created_at) VALUES (?,?,?,?,?,?)",
        (sender, recipient, chantier_id, subject.strip(), body.strip(), now_iso()))


def contacts(user: dict) -> pd.DataFrame:
    if user["role"] == "admin":
        return q("SELECT id, full_name, role FROM users WHERE active=1 AND id<>? ORDER BY full_name",
                 (int(user["id"]),))
    return q("SELECT id, full_name, role FROM users WHERE active=1 AND id<>? AND (role='admin' OR id IN "
             "(SELECT user_id FROM affectations WHERE chantier_id IN "
             "(SELECT chantier_id FROM affectations WHERE user_id=?))) ORDER BY full_name",
             (int(user["id"]), int(user["id"])))


def page_messagerie(user: dict, ch: pd.DataFrame) -> None:
    st.header("💬 Messagerie")
    uid = int(user["id"])
    t_in, t_out, t_new, t_can = st.tabs(["📥 Réception", "📤 Envoyés", "✉️ Nouveau message", "👥 Canaux de chantier"])

    with t_in:
        st.caption(f"{unread_count(uid)} message(s) non lu(s)")
        if st.button("Tout marquer comme lu"):
            run("UPDATE messages SET lu=1 WHERE recipient_id=?", (uid,))
            st.rerun()
        inbox = q("SELECT m.id, m.subject, m.body, m.created_at, m.lu, m.sender_id, u.full_name AS expediteur, "
                  "c.nom AS chantier FROM messages m JOIN users u ON u.id=m.sender_id "
                  "LEFT JOIN chantiers c ON c.id=m.chantier_id WHERE m.recipient_id=? "
                  "ORDER BY m.created_at DESC LIMIT 200", (uid,))
        if inbox.empty:
            st.info("Aucun message reçu.")
        for r in inbox.itertuples():
            titre = ("🔵 " if not r.lu else "") + f"{r.subject or '(sans objet)'} — {r.expediteur} · {r.created_at.replace('T', ' ')}"
            with st.expander(titre):
                if r.chantier:
                    st.caption(f"Chantier concerné : {r.chantier}")
                st.markdown(r.body)
                rep = st.text_area("Répondre", key=f"rep_{r.id}")
                c1, c2 = st.columns(2)
                if c1.button("Envoyer la réponse", key=f"send_{r.id}"):
                    if rep.strip():
                        send_direct(uid, int(r.sender_id), "Re: " + (r.subject or ""), rep)
                        run("UPDATE messages SET lu=1 WHERE id=? AND recipient_id=?", (int(r.id), uid))
                        st.rerun()
                    else:
                        st.error("Écrivez une réponse.")
                if not r.lu and c2.button("Marquer comme lu", key=f"read_{r.id}"):
                    run("UPDATE messages SET lu=1 WHERE id=? AND recipient_id=?", (int(r.id), uid))
                    st.rerun()

    with t_out:
        sent = q("SELECT m.subject, m.body, m.created_at, u.full_name AS destinataire FROM messages m "
                 "JOIN users u ON u.id=m.recipient_id WHERE m.sender_id=? ORDER BY m.created_at DESC LIMIT 200", (uid,))
        if sent.empty:
            st.info("Aucun message envoyé.")
        for r in sent.itertuples():
            with st.expander(f"{r.subject or '(sans objet)'} → {r.destinataire} · {r.created_at.replace('T', ' ')}"):
                st.markdown(r.body)

    with t_new:
        ct = contacts(user)
        if ct.empty:
            st.info("Aucun interlocuteur disponible (vous n'avez pas de chantier en commun avec d'autres membres).")
        else:
            opts = {int(r.id): f"{r.full_name} — {ROLES[r.role]}" for r in ct.itertuples()}
            names = names_map(ch)
            with st.form("form_msg", clear_on_submit=True):
                dest = st.selectbox("Destinataire", list(opts), format_func=opts.get)
                ref = st.selectbox("Chantier concerné (facultatif)", [None] + list(names),
                                   format_func=lambda x: "—" if x is None else names[x])
                sujet = st.text_input("Objet")
                corps = st.text_area("Message")
                ok = st.form_submit_button("Envoyer")
            if ok:
                if not corps.strip():
                    st.error("Le message est vide.")
                else:
                    send_direct(uid, int(dest), sujet, corps, ref)
                    st.success("Message envoyé.")

    with t_can:
        if ch.empty:
            st.info("Aucun chantier affecté.")
        else:
            names = names_map(ch)
            cid = st.selectbox("Canal du chantier", list(names), format_func=names.get, key="canal_ch")
            guard_access(cid, ch)
            msgs = q("SELECT m.body, m.created_at, u.full_name, u.role FROM messages m JOIN users u ON u.id=m.sender_id "
                     "WHERE m.chantier_id=? AND m.recipient_id IS NULL ORDER BY m.created_at DESC LIMIT 100", (int(cid),))
            for r in msgs.iloc[::-1].itertuples():
                with st.chat_message(r.full_name, avatar=ROLE_ICONS.get(r.role, "👤")):
                    st.caption(f"{r.full_name} — {ROLES[r.role]} · {r.created_at.replace('T', ' ')}")
                    st.markdown(r.body)
            texte = st.chat_input("Écrire à toute l'équipe du chantier…", key="canal_input")
            if texte and texte.strip():
                run("INSERT INTO messages(sender_id,recipient_id,chantier_id,body,created_at) VALUES (?,NULL,?,?,?)",
                    (uid, int(cid), texte.strip(), now_iso()))
                st.rerun()


# --- Administration ---------------------------------------------------------
def page_chantiers(user: dict, ch: pd.DataFrame) -> None:
    st.header("🏗️ Chantiers")
    if not ch.empty:
        d = ch.copy()
        d["budget_prevu"] = d["budget_prevu"].map(money)
        show_df(d.drop(columns=["id"]))

    if can("chantier_creer"):
        with st.expander("➕ Créer un chantier"):
            with st.form("form_chantier", clear_on_submit=True):
                nom = st.text_input("Nom du chantier")
                c1, c2 = st.columns(2)
                loc = c1.text_input("Localisation")
                typ = c2.selectbox("Type d'ouvrage", TYPES_OUVRAGE)
                budget = st.number_input(f"Budget prévu ({DEVISE})", 0.0, step=1_000_000.0)
                c3, c4 = st.columns(2)
                debut = c3.date_input("Début", value=date.today())
                fin = c4.date_input("Fin prévue", value=date.today() + timedelta(days=365))
                ok = st.form_submit_button("Créer")
            if ok:
                if not nom.strip():
                    st.error("Le nom est obligatoire.")
                elif fin <= debut:
                    st.error("La date de fin doit être postérieure au début.")
                else:
                    cid = run("INSERT INTO chantiers(nom,localisation,type_ouvrage,budget_prevu,date_debut,date_fin_prevue) "
                              "VALUES (?,?,?,?,?,?)", (nom.strip(), loc.strip(), typ, budget, debut.isoformat(), fin.isoformat()))
                    if user["role"] != "admin":
                        run("INSERT OR IGNORE INTO affectations VALUES (?,?)", (int(user["id"]), cid))
                    st.success("Chantier créé.")
                    st.rerun()

    if can("chantier_affecter") and not ch.empty:
        with st.expander("👥 Affectations et statut"):
            names = names_map(ch)
            cid = st.selectbox("Chantier", list(names), format_func=names.get, key="aff_ch")
            guard_access(cid, ch)
            membres = q("SELECT id, full_name, role FROM users WHERE active=1 AND role<>'admin' ORDER BY full_name")
            opts = {int(r.id): f"{r.full_name} — {ROLES[r.role]}" for r in membres.itertuples()}
            actuels = [int(x) for x in q("SELECT user_id FROM affectations WHERE chantier_id=?", (int(cid),))["user_id"]]
            choisis = st.multiselect("Équipe du chantier", list(opts), default=[a for a in actuels if a in opts],
                                     format_func=opts.get, key=f"aff_{cid}")
            statut_actuel = ch[ch["id"] == cid].iloc[0]["statut"]
            statut = st.selectbox("Statut", STATUTS_CHANTIER, index=STATUTS_CHANTIER.index(statut_actuel))
            if st.button("Enregistrer"):
                final = set(choisis)
                if user["role"] != "admin":
                    final.add(int(user["id"]))  # on ne se retire pas soi-même
                stmts = [("DELETE FROM affectations WHERE chantier_id=?", (int(cid),))]
                stmts += [("INSERT INTO affectations VALUES (?,?)", (int(u), int(cid))) for u in final]
                stmts.append(("UPDATE chantiers SET statut=? WHERE id=?", (statut, int(cid))))
                run_many(stmts)
                st.success("Mis à jour.")
                st.rerun()


def page_users(user: dict, ch: pd.DataFrame) -> None:
    st.header("🛠️ Utilisateurs")
    users = q("SELECT id, username, full_name, role, active, must_change, created_at FROM users ORDER BY role, full_name")
    show_df(users.assign(role=users["role"].map(ROLES), active=users["active"].map({1: "Actif", 0: "Désactivé"}))
            .drop(columns=["id"]))

    with st.expander("➕ Créer un compte"):
        chantiers = q("SELECT id, nom FROM chantiers ORDER BY nom")
        cn = {int(r.id): r.nom for r in chantiers.itertuples()}
        with st.form("form_user", clear_on_submit=True):
            c1, c2 = st.columns(2)
            username = c1.text_input("Identifiant")
            full = c2.text_input("Nom complet")
            role = st.selectbox("Profil", list(ROLES), format_func=ROLES.get)
            pwd = st.text_input("Mot de passe temporaire", type="password")
            aff = st.multiselect("Chantiers affectés", list(cn), format_func=cn.get)
            ok = st.form_submit_button("Créer le compte")
        if ok:
            if not username.strip() or not full.strip():
                st.error("Identifiant et nom sont obligatoires.")
            elif len(pwd) < MIN_PASSWORD:
                st.error(f"Le mot de passe doit contenir au moins {MIN_PASSWORD} caractères.")
            else:
                try:
                    uid = run("INSERT INTO users(username,password_hash,full_name,role,must_change) VALUES (?,?,?,?,1)",
                              (username.strip(), hash_pw(pwd), full.strip(), role))
                    if role != "admin":
                        run_many([("INSERT INTO affectations VALUES (?,?)", (uid, int(c))) for c in aff])
                    st.success("Compte créé (changement de mot de passe exigé à la première connexion).")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Cet identifiant existe déjà.")

    with st.expander("✏️ Modifier un compte"):
        opts = {int(r.id): f"{r.username} — {r.full_name}" for r in users.itertuples()}
        uid = st.selectbox("Compte", list(opts), format_func=opts.get)
        c1, c2 = st.columns(2)
        new_pwd = c1.text_input("Nouveau mot de passe temporaire", type="password", key="adm_newpwd")
        if c1.button("Réinitialiser le mot de passe"):
            if len(new_pwd) < MIN_PASSWORD:
                st.error(f"Au moins {MIN_PASSWORD} caractères.")
            else:
                run("UPDATE users SET password_hash=?, must_change=1 WHERE id=?", (hash_pw(new_pwd), int(uid)))
                st.success("Mot de passe réinitialisé.")
        actif = bool(users[users["id"] == uid].iloc[0]["active"])
        if uid == int(user["id"]):
            c2.caption("Vous ne pouvez pas désactiver votre propre compte.")
        elif c2.button("Désactiver" if actif else "Réactiver"):
            run("UPDATE users SET active=? WHERE id=?", (0 if actif else 1, int(uid)))
            st.rerun()


def page_compte(user: dict, ch: pd.DataFrame) -> None:
    st.header("👤 Mon compte")
    st.write(f"**{user['full_name']}** — {ROLES[user['role']]} (`{user['username']}`)")
    with st.form("form_pwd", clear_on_submit=True):
        old = st.text_input("Mot de passe actuel", type="password")
        new = st.text_input("Nouveau mot de passe", type="password")
        new2 = st.text_input("Confirmer le nouveau mot de passe", type="password")
        ok = st.form_submit_button("Changer le mot de passe")
    if ok:
        row = one("SELECT password_hash FROM users WHERE id=?", (int(user["id"]),))
        if not row or not check_pw(old, row["password_hash"]):
            st.error("Mot de passe actuel incorrect.")
        elif len(new) < MIN_PASSWORD:
            st.error(f"Au moins {MIN_PASSWORD} caractères.")
        elif new != new2:
            st.error("Les deux saisies ne correspondent pas.")
        elif new == old:
            st.error("Le nouveau mot de passe doit être différent.")
        else:
            run("UPDATE users SET password_hash=?, must_change=0 WHERE id=?", (hash_pw(new), int(user["id"])))
            st.success("Mot de passe modifié.")
            st.rerun()
    st.subheader("Droits de votre profil")
    st.markdown("\n".join(f"- {PERMISSION_LABELS[p]}" for p in PERMISSION_LABELS if p in PERMISSIONS[user["role"]]))


# ----------------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------------
def build_menu(user: dict) -> list[tuple[str, str, callable]]:
    menu = []
    if can("dashboard"):
        menu.append(("dashboard", "📊 Tableau de bord", page_dashboard))
    if can("journal_lire"):
        menu.append(("journal", "📓 Journal de chantier", page_journal))
    if can("couts_lire") or can("couts_ecrire"):
        menu.append(("couts", "💰 Dépenses", page_couts))
    if can("essais_lire"):
        menu.append(("essais", "🧪 Essais & qualité", page_essais))
    if can("incidents_lire"):
        menu.append(("incidents", "⚠️ Incidents & sécurité", page_incidents))
    if can("analyses"):
        menu.append(("analyses", "📈 Analyses", page_analyses))
    if can("messagerie"):
        n = unread_count(int(user["id"]))
        menu.append(("messagerie", "💬 Messagerie" + (f" ({n})" if n else ""), page_messagerie))
    if can("chantier_creer") or can("chantier_affecter"):
        menu.append(("chantiers", "🏗️ Chantiers", page_chantiers))
    if can("users_gerer"):
        menu.append(("users", "🛠️ Utilisateurs", page_users))
    menu.append(("compte", "👤 Mon compte", page_compte))
    return menu


def main() -> None:
    st.set_page_config(page_title="Screenlead", page_icon="🏗️", layout="wide")
    bootstrap()
    user = refresh_user()
    if not user:
        login_page()
        return

    menu = build_menu(user)
    labels = {k: lbl for k, lbl, _ in menu}
    pages = {k: fn for k, _, fn in menu}
    with st.sidebar:
        st.title("🏗️ Screenlead")
        st.write(f"{ROLE_ICONS[user['role']]} **{user['full_name']}**")
        st.caption(ROLES[user["role"]])
        if user["must_change"]:
            st.warning("Mot de passe temporaire : changez-le dans « Mon compte ».")
        choice = st.radio("Navigation", list(labels), format_func=labels.get, label_visibility="collapsed")
        if st.button("Se déconnecter"):
            st.session_state.clear()
            st.rerun()
    pages[choice](user, chantiers_visibles(user))


main()
