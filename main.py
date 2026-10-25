"""Innovation BTP - application de gestion de chantiers (Streamlit).

Points clés de cette version :
* Aucune utilisation de ``signal.signal`` : l'erreur
  « signal only works in main thread of the main interpreter » ne peut plus venir
  de ce fichier. La connexion à la base est créée UNE seule fois avec
  ``st.cache_resource`` et ``check_same_thread=False`` (compatible Streamlit Cloud).
* Connexion sécurisée (mots de passe hachés PBKDF2, rôles admin / gestionnaire / lecteur).
* Modules : tableau de bord, chantiers, intervenants, clients, devis, marchés BTP,
  stock, personnel, dépenses, rapports/exports, paramètres.

NOUVEAUTÉS
* Page d'accueil animée : bâtiments, grue, camions de location et symboles de probabilité ;
  au premier lancement, 4 chantiers « En cours » sont créés avec leurs équipes complètes.
* « Intervenants » : fiche par chantier (ingénieur, technicien, manœuvres, plombiers,
  ferrailleurs, contrôleurs techniques) avec n°, nom, téléphone, taux journalier.
* « Marchés BTP » : appels d'offres, prix des matériaux, offres des concurrents,
  probabilité de gain et prix conseillé (maximise le gain espéré), simulateur.

Lancer en local :  streamlit run app.py
"""
from __future__ import annotations

import hashlib
import hmac
import html
import io
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

# --------------------------------------------------------------------------- #
# Configuration générale
# --------------------------------------------------------------------------- #
APP_TITLE = "Innovation BTP"
DB_PATH = Path(os.environ.get("BTP_DB", "innovation_btp.db"))
TVA_DEFAUT = 19.25  # TVA en % (Cameroun)
DEVISE = "FCFA"
PBKDF2_ITERATIONS = 200_000

STATUTS_CHANTIER = ["Planifié", "En cours", "Suspendu", "Terminé", "Annulé"]
STATUTS_DEVIS = ["Brouillon", "Envoyé", "Accepté", "Refusé"]
CATEGORIES_DEPENSE = [
    "Matériaux",
    "Main-d'œuvre",
    "Location d'engins",
    "Transport",
    "Sous-traitance",
    "Administratif",
    "Autre",
]
UNITES = ["u", "m", "m²", "m³", "kg", "t", "sac", "L", "h", "forfait"]
ROLES = ["admin", "gestionnaire", "lecteur"]
HEURES_PAR_JOUR = 8

# --- Intervenants de chantier ---
FONCTIONS_CHANTIER = [
    "Maître d'ouvrage",
    "Ingénieur",
    "Technicien",
    "Contrôleur technique",
    "Coffreur",
    "Ferrailleur",
    "Plombier",
    "Manœuvre",
]

# --- Marchés BTP ---
STATUTS_AO = ["À étudier", "Soumis", "Gagné", "Perdu", "Abandonné"]
POIDS_PRIOR = 5  # nombre d'observations "virtuelles" donné à l'hypothèse de départ

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("innovation_btp")

_WRITE_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'lecteur',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT NOT NULL,
    telephone TEXT,
    email TEXT,
    adresse TEXT
);
CREATE TABLE IF NOT EXISTS chantiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT NOT NULL,
    client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    localisation TEXT,
    date_debut TEXT,
    date_fin_prevue TEXT,
    budget REAL NOT NULL DEFAULT 0,
    statut TEXT NOT NULL DEFAULT 'Planifié',
    avancement INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS materiaux (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT NOT NULL UNIQUE,
    unite TEXT NOT NULL DEFAULT 'u',
    quantite REAL NOT NULL DEFAULT 0,
    seuil_alerte REAL NOT NULL DEFAULT 0,
    prix_unitaire REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mouvements_stock (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    materiau_id INTEGER NOT NULL REFERENCES materiaux(id) ON DELETE CASCADE,
    chantier_id INTEGER REFERENCES chantiers(id) ON DELETE SET NULL,
    type TEXT NOT NULL CHECK (type IN ('Entrée', 'Sortie')),
    quantite REAL NOT NULL CHECK (quantite > 0),
    date TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS employes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT NOT NULL,
    poste TEXT,
    telephone TEXT,
    taux_journalier REAL NOT NULL DEFAULT 0,
    actif INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS pointages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employe_id INTEGER NOT NULL REFERENCES employes(id) ON DELETE CASCADE,
    chantier_id INTEGER REFERENCES chantiers(id) ON DELETE SET NULL,
    date TEXT NOT NULL,
    heures REAL NOT NULL CHECK (heures > 0 AND heures <= 24)
);
CREATE TABLE IF NOT EXISTS depenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chantier_id INTEGER REFERENCES chantiers(id) ON DELETE SET NULL,
    categorie TEXT NOT NULL,
    description TEXT,
    montant REAL NOT NULL CHECK (montant >= 0),
    date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    numero TEXT NOT NULL UNIQUE,
    client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    chantier_id INTEGER REFERENCES chantiers(id) ON DELETE SET NULL,
    date TEXT NOT NULL,
    statut TEXT NOT NULL DEFAULT 'Brouillon',
    tva REAL NOT NULL DEFAULT 19.25
);
CREATE TABLE IF NOT EXISTS devis_lignes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    devis_id INTEGER NOT NULL REFERENCES devis(id) ON DELETE CASCADE,
    designation TEXT NOT NULL,
    quantite REAL NOT NULL CHECK (quantite > 0),
    unite TEXT NOT NULL DEFAULT 'u',
    prix_unitaire REAL NOT NULL CHECK (prix_unitaire >= 0)
);
CREATE TABLE IF NOT EXISTS affectations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chantier_id INTEGER NOT NULL REFERENCES chantiers(id) ON DELETE CASCADE,
    employe_id INTEGER NOT NULL REFERENCES employes(id) ON DELETE CASCADE,
    fonction TEXT NOT NULL,
    UNIQUE (chantier_id, employe_id)
);
CREATE TABLE IF NOT EXISTS appels_offres (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reference TEXT NOT NULL UNIQUE,
    intitule TEXT NOT NULL,
    maitre_ouvrage TEXT,
    localisation TEXT,
    date_limite TEXT,
    estimation REAL NOT NULL DEFAULT 0,
    cout_estime REAL NOT NULL DEFAULT 0,
    notre_offre REAL NOT NULL DEFAULT 0,
    nb_concurrents INTEGER NOT NULL DEFAULT 4,
    statut TEXT NOT NULL DEFAULT 'À étudier',
    attributaire TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS concurrents_offres (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appel_id INTEGER NOT NULL REFERENCES appels_offres(id) ON DELETE CASCADE,
    entreprise TEXT NOT NULL,
    montant REAL NOT NULL CHECK (montant > 0)
);
CREATE TABLE IF NOT EXISTS prix_marche (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    materiau TEXT NOT NULL,
    unite TEXT NOT NULL DEFAULT 'u',
    fournisseur TEXT NOT NULL,
    prix REAL NOT NULL CHECK (prix > 0),
    date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


# --------------------------------------------------------------------------- #
# Sécurité : mots de passe
# --------------------------------------------------------------------------- #
def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Retourne (hash_hex, sel_hex) via PBKDF2-HMAC-SHA256."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return digest.hex(), salt


def verify_password(password: str, expected_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, expected_hash)


def password_problems(password: str) -> list[str]:
    """Liste les règles non respectées par un mot de passe."""
    problems = []
    if len(password) < 8:
        problems.append("au moins 8 caractères")
    if not re.search(r"[A-Za-z]", password):
        problems.append("au moins une lettre")
    if not re.search(r"\d", password):
        problems.append("au moins un chiffre")
    return problems


# --------------------------------------------------------------------------- #
# Accès base de données
# --------------------------------------------------------------------------- #
@st.cache_resource(show_spinner=False)
def get_connection() -> sqlite3.Connection:
    """Connexion unique partagée (créée une seule fois, sans gestion de signaux)."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:  # système de fichiers sans WAL
        log.warning("Mode WAL indisponible, utilisation du mode par défaut.")
    conn.executescript(SCHEMA)
    ensure_admin(conn)
    conn.commit()
    return conn


def ensure_schema() -> None:
    """Crée les tables manquantes (sans toucher aux données) : utile quand la connexion en cache est ancienne."""
    conn = get_connection()
    with _WRITE_LOCK:
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        except sqlite3.Error:
            log.exception("Mise à jour du schéma impossible")


def default_admin_password() -> str:
    try:
        if "BTP_ADMIN_PASSWORD" in st.secrets:
            return str(st.secrets["BTP_ADMIN_PASSWORD"])
    except Exception:  # pas de fichier secrets
        pass
    return os.environ.get("BTP_ADMIN_PASSWORD", "ChangeMe123")


def ensure_admin(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        pwd_hash, salt = hash_password(default_admin_password())
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, role, created_at) "
            "VALUES (?, ?, ?, 'admin', ?)",
            ("admin", pwd_hash, salt, datetime.now().isoformat(timespec="seconds")),
        )
        log.info("Compte administrateur initial créé.")


def db_write(sql: str, params: tuple | dict = ()) -> int | None:
    """Exécute une écriture. Retourne lastrowid, ou None si une erreur survient."""
    conn = get_connection()
    with _WRITE_LOCK:
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            st.error(f"Donnée refusée par la base (doublon ou lien invalide) : {exc}")
        except sqlite3.Error as exc:
            conn.rollback()
            log.exception("Erreur SQL")
            st.error(f"Erreur base de données : {exc}")
    return None


def db_transaction(statements: list[tuple[str, tuple]]) -> bool:
    """Exécute plusieurs requêtes de façon atomique."""
    conn = get_connection()
    with _WRITE_LOCK:
        try:
            for sql, params in statements:
                conn.execute(sql, params)
            conn.commit()
            return True
        except sqlite3.Error as exc:
            conn.rollback()
            log.exception("Transaction annulée")
            st.error(f"Opération annulée : {exc}")
            return False


def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    try:
        return pd.read_sql_query(sql, get_connection(), params=params)
    except Exception as exc:  # noqa: BLE001
        log.exception("Erreur de lecture")
        st.error(f"Impossible de lire les données : {exc}")
        return pd.DataFrame()


def choice_map(sql: str) -> dict[int, str]:
    """Requête (id, libellé) -> dictionnaire {id: libellé}."""
    df = query(sql)
    if df.empty:
        return {}
    return {int(r.iloc[0]): str(r.iloc[1]) for _, r in df.iterrows()}


# --------------------------------------------------------------------------- #
# Utilitaires
# --------------------------------------------------------------------------- #
def fcfa(value: float | int | None) -> str:
    value = 0 if value is None or pd.isna(value) else value
    return f"{value:,.0f}".replace(",", " ") + f" {DEVISE}"


def to_date(value: str | None, default: date | None = None) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return default or date.today()


def valid_email(email: str) -> bool:
    return not email or re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is not None


def valid_phone(phone: str) -> bool:
    return not phone or re.fullmatch(r"\+?[\d\s().-]{6,20}", phone) is not None


def flash(message: str) -> None:
    st.session_state["_flash"] = message


def show_flash() -> None:
    message = st.session_state.pop("_flash", None)
    if message:
        st.toast(message)


def current_user() -> dict | None:
    return st.session_state.get("user")


def can_edit() -> bool:
    user = current_user()
    return bool(user) and user["role"] in ("admin", "gestionnaire")


def is_admin() -> bool:
    user = current_user()
    return bool(user) and user["role"] == "admin"


def read_only_notice() -> None:
    st.info("Votre rôle est en lecture seule : modification impossible.")


def confirm_delete(key: str, label: str = "Je confirme la suppression") -> bool:
    return st.checkbox(label, key=f"confirm_{key}")


def excel_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name[:31], index=False)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Authentification
# --------------------------------------------------------------------------- #
def login_page() -> None:
    render_hero()
    _, centre, _ = st.columns([1, 1.6, 1])
    with centre:
        st.subheader("Connexion")
        st.caption("Connectez-vous pour accéder à la gestion de vos chantiers et à l'analyse des marchés BTP.")
        attempts = st.session_state.get("login_attempts", 0)
        if attempts >= 5:
            st.error("Trop de tentatives échouées. Rechargez la page pour réessayer.")
            return
        with st.form("login_form"):
            username = st.text_input("Nom d'utilisateur")
            password = st.text_input("Mot de passe", type="password")
            submitted = st.form_submit_button("Se connecter", type="primary")
        if submitted:
            df = query("SELECT * FROM users WHERE username = ?", (username.strip(),))
            if not df.empty:
                row = df.iloc[0]
                if verify_password(password, row["password_hash"], row["salt"]):
                    st.session_state["user"] = {
                        "id": int(row["id"]),
                        "username": row["username"],
                        "role": row["role"],
                    }
                    st.session_state["login_attempts"] = 0
                    st.rerun()
            st.session_state["login_attempts"] = attempts + 1
            st.error("Identifiants incorrects.")
        if default_admin_password() == "ChangeMe123":
            st.warning(
                "Première utilisation : compte « admin » / mot de passe « ChangeMe123 ». "
                "Changez-le dans Paramètres dès la connexion."
            )


def logout() -> None:
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.rerun()


# --------------------------------------------------------------------------- #
# Scène d'accueil animée (bâtiments, grue, camions de location, probabilités)
# --------------------------------------------------------------------------- #
HERO_CSS = """
html,body{margin:0;padding:0;background:transparent;overflow:hidden}
.wrap{border-radius:14px;overflow:hidden;box-shadow:0 2px 14px rgba(0,0,0,.28)}
svg{display:block;width:100%;height:380px;font-family:Arial,Helvetica,sans-serif}
@keyframes driveR{from{transform:translateX(-240px)}to{transform:translateX(1340px)}}
@keyframes driveL{from{transform:translateX(1340px)}to{transform:translateX(-240px)}}
@keyframes cloud{from{transform:translateX(-200px)}to{transform:translateX(1400px)}}
@keyframes floaty{0%,100%{transform:translateY(0)}50%{transform:translateY(-7px)}}
@keyframes swing{from{transform:scaleX(1)}to{transform:scaleX(-.55)}}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
@keyframes road{to{stroke-dashoffset:-60}}
@keyframes grow{from{transform:scaleY(.1)}to{transform:scaleY(1)}}
@keyframes gauge{from{stroke-dashoffset:170}to{stroke-dashoffset:var(--off)}}
.t1{animation:driveR 20s linear infinite}
.t2{animation:driveL 24s linear infinite}
.t3{animation:driveR 20s linear infinite;animation-delay:-10s}
.t4{animation:driveL 24s linear infinite;animation-delay:-12s}
.cloud{animation:cloud 60s linear infinite}
.float{animation:floaty 4.5s ease-in-out infinite}
.jib{animation:swing 9s ease-in-out infinite alternate}
.win{animation:blink 5s ease-in-out infinite}
.road{stroke-dasharray:30 30;animation:road 1.2s linear infinite}
.bar{transform-box:fill-box;transform-origin:bottom;animation:grow 2.4s ease-out infinite alternate}
.gauge{stroke-dasharray:170;animation:gauge 2.6s ease-out forwards}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes armswing{from{transform:rotate(-7deg)}to{transform:rotate(9deg)}}
.gear{transform-box:fill-box;transform-origin:center;animation:spin 14s linear infinite}
.gear.rev{animation-direction:reverse}
.arm{transform-box:fill-box;transform-origin:right bottom;animation:armswing 2.6s ease-in-out infinite alternate}
.pulse{animation:blink 1.6s ease-in-out infinite}
.banner svg{height:200px}
@media (prefers-reduced-motion:reduce){*{animation:none!important}.gauge{stroke-dashoffset:var(--off)}}
"""


def _truck_svg(body: str, cab: str, direction: int, cls: str, y: int, scale: float = 1.0) -> str:
    """Camion Bil-BTP Construction. direction = 1 vers la droite, -1 vers la gauche."""
    flip = 'transform="translate(130,0) scale(-1,1)"' if direction < 0 else ""
    text_x = 85 if direction < 0 else 45
    wheels = "".join(
        f'<circle cx="{cx}" cy="-4" r="10" fill="#1d2025"/><circle cx="{cx}" cy="-4" r="4" fill="#9aa3ad"/>'
        for cx in (22, 62, 112)
    )
    return (
        f'<g class="{cls}"><g transform="translate(0,{y}) scale({scale})">'
        f"<g {flip}>"
        f'<rect x="0" y="-52" width="92" height="44" rx="4" fill="{body}"/>'
        f'<rect x="0" y="-18" width="92" height="5" fill="rgba(0,0,0,.18)"/>'
        f'<rect x="94" y="-40" width="36" height="32" rx="5" fill="{cab}"/>'
        f'<path d="M101 -37 h17 l8 14 h-25z" fill="#cfeaff"/>'
        f'<rect x="0" y="-12" width="130" height="6" rx="2" fill="#2b2f36"/>'
        f"{wheels}</g>"
        f'<text x="{text_x}" y="-33" text-anchor="middle" font-size="14" font-weight="800" fill="#fff">BIL-BTP</text>'
        f'<text x="{text_x}" y="-21" text-anchor="middle" font-size="8.4" font-weight="700" fill="#fff">CONSTRUCTION</text>'
        f"</g></g>"
    )


def _machine_svg(kind: str, direction: int, cls: str, y: int, scale: float = 1.0) -> str:
    """Engin de chantier : « roller » (rouleau compresseur) ou « backhoe » (tractopelle)."""
    flip = 'transform="translate(130,0) scale(-1,1)"' if direction < 0 else ""
    if kind == "roller":
        text_x = 65
        shapes = (
            '<rect x="22" y="-40" width="86" height="18" rx="4" fill="#f2b705"/>'
            '<rect x="40" y="-48" width="52" height="10" rx="3" fill="#d99a00"/>'
            '<rect x="44" y="-76" width="46" height="4" rx="2" fill="#4a4f57"/>'
            '<rect x="47" y="-74" width="3" height="34" fill="#4a4f57"/><rect x="84" y="-74" width="3" height="34" fill="#4a4f57"/>'
            '<rect x="60" y="-54" width="12" height="14" rx="3" fill="#2a7de1"/>'
            '<circle cx="66" cy="-60" r="6" fill="#d9a77c"/><path d="M60 -61 a6 6 0 0 1 12 0z" fill="#fff"/>'
            '<circle cx="22" cy="-17" r="17" fill="#8d949c"/><circle cx="22" cy="-17" r="6" fill="#4a4f57"/>'
            '<circle cx="108" cy="-17" r="17" fill="#8d949c"/><circle cx="108" cy="-17" r="6" fill="#4a4f57"/>'
            '<path d="M22 -34 v34 M5 -17 h34 M108 -34 v34 M91 -17 h34" stroke="#6b7178" stroke-width="2"/>'
        )
    else:
        text_x = 68 if direction < 0 else 62
        shapes = (
            '<path d="M96 -40 L128 -26" stroke="#f2b705" stroke-width="6" stroke-linecap="round"/>'
            '<path d="M122 -38 L142 -38 L138 -14 L122 -14 Z" fill="#d99a00" stroke="#8a6200"/>'
            '<rect x="18" y="-46" width="86" height="24" rx="5" fill="#f2b705"/>'
            '<rect x="40" y="-78" width="40" height="34" rx="4" fill="#f2b705"/>'
            '<rect x="45" y="-73" width="30" height="22" rx="2" fill="#cfeaff"/>'
            '<circle cx="60" cy="-60" r="6" fill="#d9a77c"/><path d="M54 -61 a6 6 0 0 1 12 0z" fill="#fff"/>'
            '<g class="arm"><path d="M20 -44 L-6 -72 L-26 -34" fill="none" stroke="#f2b705" stroke-width="6" '
            'stroke-linejoin="round" stroke-linecap="round"/>'
            '<path d="M-34 -36 L-18 -36 L-22 -16 L-38 -20 Z" fill="#d99a00" stroke="#8a6200"/></g>'
            '<circle cx="38" cy="-17" r="17" fill="#1d2025"/><circle cx="38" cy="-17" r="7" fill="#9aa3ad"/>'
            '<circle cx="100" cy="-12" r="12" fill="#1d2025"/><circle cx="100" cy="-12" r="5" fill="#9aa3ad"/>'
        )
    return (
        f'<g class="{cls}"><g transform="translate(0,{y}) scale({scale})">'
        f"<g {flip}>{shapes}</g>"
        f'<text x="{text_x}" y="-28" text-anchor="middle" font-size="9" font-weight="800" fill="#3a2f00">BIL-BTP</text>'
        f"</g></g>"
    )


def hero_html(chantiers: list[tuple[str, int]] | None = None, proba: float | None = None) -> str:
    """Page HTML autonome : ville en construction, grue, camions de location et symboles de probabilité.

    ``chantiers`` : liste (nom, avancement %) ; chaque chantier devient un bâtiment dont la hauteur
    suit l'avancement. ``proba`` : meilleure probabilité de gain (0-100) pour la jauge, sinon symbole seul.
    """
    items = [(str(n), max(0, min(100, int(a)))) for n, a in (chantiers or [])][:6]
    if not items:
        items = [("Immeuble R+3", 45), ("Villa duplex", 60), ("Pont de desserte", 30), ("Entrepôt", 70)]
    n = len(items)
    zone_x, zone_w, ground = 30, 840, 300
    slot = zone_w / n
    bw = min(120.0, slot - 26)
    palette = ["#5b7fa6", "#7a8fb5", "#6c9a8b", "#a58b6f", "#8a7ca8", "#6f98a5"]
    crane_idx = min(range(n), key=lambda i: items[i][1])
    parts: list[str] = []
    crane = ""

    for i, (nom, av) in enumerate(items):
        x = zone_x + i * slot + (slot - bw) / 2
        h = 60 + 1.4 * av
        top = ground - h
        if av < 100:
            parts.append(
                f'<rect x="{x:.1f}" y="{ground - 200}" width="{bw:.1f}" height="200" fill="none" '
                f'stroke="#ffffff" stroke-opacity=".55" stroke-dasharray="6 5" rx="3"/>'
            )
        parts.append(f'<rect x="{x:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{h:.1f}" fill="{palette[i % 6]}" rx="3"/>')
        parts.append(f'<rect x="{x - 3:.1f}" y="{top - 5:.1f}" width="{bw + 6:.1f}" height="7" fill="#2f3b4a" rx="2"/>')
        floors = int((h - 14) // 24)
        cols = max(int((bw - 12) // 24), 2)
        spacing = (bw - 20) / cols
        for r in range(floors):
            for c in range(cols):
                wx = x + 10 + c * spacing + (spacing - 12) / 2
                wy = top + 12 + r * 24
                if wy + 14 > ground - 4:
                    continue
                off = (r + c + i) % 4 == 0
                delay = ((i * 7 + r * 3 + c * 5) % 9) * 0.45
                fill = "#26303b" if off else "#ffe38a"
                cls = "" if off else ' class="win"'
                parts.append(
                    f'<rect{cls} x="{wx:.1f}" y="{wy:.1f}" width="12" height="14" rx="1.5" fill="{fill}" '
                    f'style="animation-delay:{delay:.2f}s"/>'
                )
        label = nom if len(nom) <= 26 else nom[:25] + "…"
        parts.append(
            f'<text x="{x + bw / 2:.1f}" y="{top - 12:.1f}" text-anchor="middle" font-size="10.5" font-weight="700" '
            f'fill="#fff" stroke="#14202e" stroke-width="3" paint-order="stroke">{html.escape(label)} · {av}%</text>'
        )
        if i == crane_idx:
            tx = x + bw + 9
            crane = (
                f'<rect x="{tx - 3:.1f}" y="84" width="6" height="{ground - 84}" fill="#e8b923"/>'
                f'<rect x="{tx - 3:.1f}" y="84" width="6" height="{ground - 84}" fill="none" stroke="#7a5b00" '
                f'stroke-dasharray="3 4"/>'
                f'<g class="jib" style="transform-origin:{tx:.1f}px 84px">'
                f'<rect x="{tx - 82:.1f}" y="79" width="180" height="5" fill="#e8b923"/>'
                f'<rect x="{tx - 94:.1f}" y="75" width="15" height="15" fill="#4a4f57"/>'
                f'<line x1="{tx + 74:.1f}" y1="84" x2="{tx + 74:.1f}" y2="138" stroke="#222" stroke-width="1.5"/>'
                f'<rect x="{tx + 67:.1f}" y="138" width="14" height="10" rx="2" fill="#e8b923"/></g>'
            )

    skyline = "".join(
        f'<rect x="{sx}" y="{ground - sh}" width="{sw}" height="{sh}" fill="#ffffff" fill-opacity=".16"/>'
        for sx, sw, sh in [(0, 60, 90), (70, 40, 130), (120, 70, 100), (210, 50, 150), (880, 60, 110),
                           (950, 50, 140), (1010, 70, 95), (1090, 55, 125), (1150, 60, 105)]
    )
    best = f"{proba:.0f}%" if proba is not None else "%"
    gauge_off = 170 * (1 - (proba / 100 if proba is not None else 0.72))
    trucks = (
        _truck_svg("#1f6fd1", "#174f98", 1, "t1", 330)
        + _truck_svg("#f08a24", "#c26a14", -1, "t2", 352)
        + _machine_svg("roller", 1, "t3", 330)
        + _machine_svg("backhoe", -1, "t4", 352)
    )
    bars = "".join(
        f'<rect class="bar" x="{1062 + k * 21}" y="{104 - bh}" width="14" height="{bh}" rx="2" fill="#2a7de1" '
        f'style="animation-delay:{k * 0.25:.2f}s"/>'
        for k, bh in enumerate([22, 38, 30, 50, 64])
    )
    cards = f"""
<g class="float"><rect x="890" y="26" width="140" height="94" rx="12" fill="#fff" fill-opacity=".93"/>
  <path d="M904 100 C922 100 934 46 960 46 S998 100 1016 100 Z" fill="#cfe3fb" stroke="#2a7de1" stroke-width="2"/>
  <path d="M968 100 L968 52 C986 56 1000 100 1016 100 Z" fill="#2a7de1" fill-opacity=".55"/>
  <line x1="968" y1="46" x2="968" y2="100" stroke="#c0392b" stroke-width="1.5" stroke-dasharray="3 3"/>
  <text x="960" y="114" text-anchor="middle" font-size="9.5" font-weight="700" fill="#27415f">μ ± σ · prix des concurrents</text></g>
<g class="float" style="animation-delay:-1.2s"><rect x="1044" y="26" width="140" height="94" rx="12" fill="#fff" fill-opacity=".93"/>
  {bars}<line x1="1056" y1="104" x2="1176" y2="104" stroke="#8aa0b8"/>
  <text x="1114" y="42" text-anchor="middle" font-size="10" font-weight="700" fill="#27415f">Gain espéré</text></g>
<g class="float" style="animation-delay:-2.1s"><rect x="890" y="134" width="96" height="104" rx="12" fill="#fff" fill-opacity=".93"/>
  <circle cx="938" cy="180" r="27" fill="none" stroke="#dbe5f1" stroke-width="9"/>
  <circle class="gauge" cx="938" cy="180" r="27" fill="none" stroke="#2e9e6b" stroke-width="9" stroke-linecap="round"
          transform="rotate(-90 938 180)" style="--off:{gauge_off:.1f}"/>
  <text x="938" y="185" text-anchor="middle" font-size="14" font-weight="800" fill="#1d3b2c">{best}</text>
  <text x="938" y="226" text-anchor="middle" font-size="9.5" font-weight="700" fill="#27415f">Proba. de gain</text></g>
<g class="float" style="animation-delay:-3s"><rect x="1000" y="134" width="184" height="104" rx="12" fill="#fff" fill-opacity=".93"/>
  <g transform="rotate(-12 1034 176)"><rect x="1016" y="156" width="36" height="36" rx="7" fill="#fff" stroke="#27415f" stroke-width="2"/>
    <circle cx="1026" cy="166" r="3" fill="#27415f"/><circle cx="1042" cy="166" r="3" fill="#27415f"/>
    <circle cx="1034" cy="174" r="3" fill="#27415f"/><circle cx="1026" cy="182" r="3" fill="#27415f"/><circle cx="1042" cy="182" r="3" fill="#27415f"/></g>
  <g transform="rotate(14 1078 180)"><rect x="1062" y="164" width="32" height="32" rx="7" fill="#fff" stroke="#27415f" stroke-width="2"/>
    <circle cx="1071" cy="173" r="3" fill="#27415f"/><circle cx="1085" cy="187" r="3" fill="#27415f"/></g>
  <polyline points="1108,206 1126,196 1140,200 1156,182 1172,170" fill="none" stroke="#2e9e6b" stroke-width="3" stroke-linecap="round"/>
  <polygon points="1176,166 1164,168 1172,178" fill="#2e9e6b"/>
  <text x="1092" y="228" text-anchor="middle" font-size="9.5" font-weight="700" fill="#27415f">Risque · Marge · Concurrence</text></g>
"""
    svg = f"""
<svg viewBox="0 0 1200 360" preserveAspectRatio="xMidYMax slice" role="img"
     aria-label="Chantiers en construction, camions Bil-BTP Construction, engins et analyse de probabilité">
<defs><linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#1f4e79"/><stop offset=".6" stop-color="#4f93cf"/><stop offset="1" stop-color="#f6d9a8"/></linearGradient></defs>
<rect width="1200" height="360" fill="url(#sky)"/>
<circle cx="760" cy="70" r="26" fill="#fff3b0" opacity=".95"/><circle cx="760" cy="70" r="44" fill="#fff3b0" opacity=".18"/>
<g class="cloud"><ellipse cx="0" cy="120" rx="46" ry="14" fill="#fff" opacity=".55"/><ellipse cx="26" cy="112" rx="30" ry="12" fill="#fff" opacity=".55"/></g>
<g class="cloud" style="animation-delay:-30s"><ellipse cx="0" cy="170" rx="38" ry="11" fill="#fff" opacity=".45"/></g>
{skyline}
<rect y="{ground}" width="1200" height="60" fill="#3a3f47"/><rect y="{ground}" width="1200" height="10" fill="#9aa0a6"/>
<line class="road" x1="0" y1="343" x2="1200" y2="343" stroke="#f2f2f2" stroke-width="2"/>
{"".join(parts)}
{crane}
{cards}
<text x="40" y="50" font-size="31" font-weight="800" fill="#fff" stroke="#14202e" stroke-opacity=".35" stroke-width="3" paint-order="stroke">INNOVATION BTP</text>
<text x="40" y="74" font-size="14" fill="#fff" stroke="#14202e" stroke-opacity=".35" stroke-width="2.5" paint-order="stroke">Chantiers · Marchés · Analyse de probabilité · Engins de chantier</text>
{trucks}
</svg>"""
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<style>{HERO_CSS}</style></head><body><div class=\"wrap\">{svg}</div></body></html>"
    )


def render_hero(chantiers: list[tuple[str, int]] | None = None, proba: float | None = None) -> None:
    components.html(hero_html(chantiers, proba), height=384)


# --------------------------------------------------------------------------- #
# Arrière-plans animés : un bandeau thématique pour chaque option de l'application
# --------------------------------------------------------------------------- #
def _windows(x: float, top: float, w: float, h: float, seed: int = 0) -> str:
    out = []
    cols = max(int((w - 10) // 18), 2)
    sp = (w - 12) / cols
    for r in range(int((h - 10) // 20)):
        for c in range(cols):
            wx = x + 6 + c * sp + (sp - 10) / 2
            wy = top + 10 + r * 20
            if wy + 12 > 166:
                continue
            lit = (r + c + seed) % 4 != 0
            cls = ' class="win"' if lit else ""
            fill = "#ffe38a" if lit else "#26303b"
            delay = ((seed * 5 + r * 3 + c * 7) % 9) * 0.45
            out.append(
                f'<rect{cls} x="{wx:.1f}" y="{wy:.1f}" width="10" height="12" rx="1.5" fill="{fill}" '
                f'style="animation-delay:{delay:.2f}s"/>'
            )
    return "".join(out)


def _worker(x: float, helmet: str, shirt: str, tool: str = "none", ground: int = 170, cls: str = "") -> str:
    tools = {
        "hammer": '<line x1="10" y1="-42" x2="24" y2="-60" stroke="#8b5a2b" stroke-width="3"/>'
        '<rect x="18" y="-66" width="14" height="7" fill="#555" transform="rotate(35 25 -62)"/>',
        "clip": '<rect x="10" y="-50" width="13" height="17" fill="#fff" stroke="#555"/>'
        '<line x1="13" y1="-45" x2="20" y2="-45" stroke="#999"/><line x1="13" y1="-40" x2="20" y2="-40" stroke="#999"/>',
        "none": "",
    }
    return (
        f'<g transform="translate({x},{ground})"><g class="{cls}">'
        f'<rect x="-8" y="-24" width="7" height="24" fill="#2f3b4a"/><rect x="1" y="-24" width="7" height="24" fill="#2f3b4a"/>'
        f'<rect x="-10" y="-52" width="20" height="30" rx="5" fill="{shirt}"/>'
        f'<rect x="-15" y="-50" width="6" height="20" rx="3" fill="{shirt}"/>'
        f'<rect x="-5" y="-46" width="10" height="6" fill="#fff"/>'
        f'<circle cx="0" cy="-62" r="9" fill="#d9a77c"/>'
        f'<path d="M-11 -64 a11 11 0 0 1 22 0 z" fill="{helmet}"/><rect x="-13" y="-65" width="26" height="3" rx="1.5" fill="{helmet}"/>'
        f"{tools[tool]}</g></g>"
    )


def _gear(cx: float, cy: float, r: float, teeth: int, fill: str, rev: bool = False) -> str:
    pts = []
    n = teeth * 4
    for k in range(n):
        ang = 2 * math.pi * k / n
        rad = r if k % 4 in (0, 1) else r * 0.78
        pts.append(f"{cx + rad * math.cos(ang):.1f},{cy + rad * math.sin(ang):.1f}")
    cls = "gear rev" if rev else "gear"
    return (
        f'<g class="{cls}"><polygon points="{" ".join(pts)}" fill="{fill}" stroke="#14202e" stroke-opacity=".4"/>'
        f'<circle cx="{cx}" cy="{cy}" r="{r * 0.34:.1f}" fill="#2f3640"/></g>'
    )


def _pie(cx: float, cy: float, r: float, parts: list[float], colors: list[str]) -> str:
    out, start = [], -math.pi / 2
    for frac, col in zip(parts, colors):
        end = start + 2 * math.pi * frac
        x1, y1 = cx + r * math.cos(start), cy + r * math.sin(start)
        x2, y2 = cx + r * math.cos(end), cy + r * math.sin(end)
        large = 1 if frac > 0.5 else 0
        out.append(f'<path d="M{cx} {cy} L{x1:.1f} {y1:.1f} A{r} {r} 0 {large} 1 {x2:.1f} {y2:.1f} Z" fill="{col}"/>')
        start = end
    return "".join(out)


def _card(x: float, y: float, w: float, h: float, delay: float = 0.0) -> str:
    return (
        f'<rect class="float" x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="#fff" fill-opacity=".93" '
        f'style="animation-delay:{delay}s"/>'
    )


def _motif_chantiers() -> str:
    out = []
    for i, (x, w, h, col) in enumerate([(650, 90, 95, "#5b7fa6"), (760, 100, 125, "#7a8fb5"), (880, 90, 70, "#6c9a8b"), (980, 85, 108, "#a58b6f")]):
        top = 170 - h
        out.append(f'<rect x="{x}" y="{top}" width="{w}" height="{h}" fill="{col}" rx="3"/>')
        out.append(f'<rect x="{x - 3}" y="{top - 5}" width="{w + 6}" height="7" fill="#2f3b4a" rx="2"/>')
        out.append(_windows(x, top, w, h, i))
    out.append(
        '<rect x="1107" y="22" width="6" height="148" fill="#e8b923"/>'
        '<g class="jib" style="transform-origin:1110px 26px"><rect x="1030" y="22" width="150" height="5" fill="#e8b923"/>'
        '<rect x="1020" y="18" width="14" height="14" fill="#4a4f57"/>'
        '<line x1="1160" y1="27" x2="1160" y2="78" stroke="#222" stroke-width="1.5"/>'
        '<rect x="1153" y="78" width="14" height="10" rx="2" fill="#e8b923"/></g>'
    )
    return "".join(out)


def _motif_equipe(personnel: bool = False) -> str:
    helmets = ["#f2c200", "#ffffff", "#ff7a1a", "#2a7de1", "#e03b3b", "#2e9e6b"]
    shirts = ["#2a7de1", "#e8e8e8", "#f08a24", "#2e9e6b", "#8a7ca8", "#e0b84a"]
    tools = ["hammer", "clip", "none", "clip", "hammer", "none"]
    xs = [680, 760, 840, 920] if personnel else [660, 740, 820, 900, 980, 1060]
    out = [
        _worker(x, helmets[i], shirts[i], tools[i], cls="float" if i % 2 else "")
        for i, x in enumerate(xs)
    ]
    if personnel:
        out.append(
            '<rect x="1010" y="24" width="160" height="112" rx="10" fill="#fff" fill-opacity=".93"/>'
            '<text x="1090" y="44" text-anchor="middle" font-size="11" font-weight="700" fill="#27415f">Pointage</text>'
            + "".join(f'<line x1="1022" y1="{54 + k * 18}" x2="1158" y2="{54 + k * 18}" stroke="#dbe5f1"/>' for k in range(5))
            + "".join(
                f'<path d="M{1030 + c * 26} {62 + r * 18} l4 5 l9 -11" fill="none" stroke="#2e9e6b" stroke-width="2.5"/>'
                for r in range(4)
                for c in range(5)
                if (r + c) % 3 != 0
            )
        )
    return "".join(out)


def _person_suit(x: float, tie: str) -> str:
    return (
        f'<g transform="translate({x},170)"><rect x="-12" y="-36" width="9" height="36" fill="#2f3b4a"/>'
        f'<rect x="3" y="-36" width="9" height="36" fill="#2f3b4a"/>'
        f'<rect x="-16" y="-84" width="32" height="50" rx="8" fill="#27415f"/>'
        f'<path d="M-6 -84 L0 -66 L6 -84 Z" fill="#fff"/><path d="M0 -78 l-3 6 l3 14 l3 -14 z" fill="{tie}"/>'
        f'<circle cx="0" cy="-98" r="12" fill="#d9a77c"/><path d="M-12 -101 a12 12 0 0 1 24 -2 z" fill="#2b2118"/></g>'
    )


def _motif_clients() -> str:
    return (
        '<polygon points="640,92 690,56 740,92" fill="#a0402f"/><rect x="650" y="92" width="80" height="78" fill="#e8d8b8"/>'
        '<rect x="680" y="126" width="20" height="44" fill="#7a4b2a"/><rect x="658" y="104" width="16" height="16" fill="#9ed0f0"/>'
        '<rect x="706" y="104" width="16" height="16" fill="#9ed0f0"/>'
        + _person_suit(800, "#c0392b")
        + _person_suit(880, "#2a7de1")
        + '<g transform="translate(0,120)"><rect x="822" y="0" width="30" height="10" rx="5" fill="#d9a77c"/>'
        '<rect x="832" y="2" width="26" height="10" rx="5" fill="#c99467"/></g>'
        '<g class="float"><rect x="950" y="36" width="110" height="118" rx="8" fill="#fff" fill-opacity=".95"/>'
        '<text x="1005" y="56" text-anchor="middle" font-size="11" font-weight="800" fill="#27415f">CONTRAT</text>'
        + "".join(f'<line x1="962" y1="{68 + k * 11}" x2="1048" y2="{68 + k * 11}" stroke="#c8d3e0" stroke-width="2"/>' for k in range(5))
        + '<path d="M964 140 q10 -22 18 -4 t18 -2 t18 -6" fill="none" stroke="#2a7de1" stroke-width="2.2"/></g>'
        '<polygon points="1090,100 1130,70 1170,100" fill="#a0402f"/><rect x="1098" y="100" width="64" height="70" fill="#e8d8b8"/>'
        '<rect x="1122" y="132" width="16" height="38" fill="#7a4b2a"/>'
    )


def _motif_devis() -> str:
    rows = "".join(
        f'<line x1="716" y1="{66 + k * 16}" x2="790" y2="{66 + k * 16}" stroke="#c8d3e0" stroke-width="3"/>'
        f'<rect x="808" y="{61 + k * 16}" width="36" height="8" rx="2" fill="#dbe5f1"/>'
        for k in range(5)
    )
    keys = "".join(
        f'<rect x="{918 + c * 26}" y="{82 + r * 22}" width="20" height="16" rx="3" fill="{"#f08a24" if c == 3 else "#dbe5f1"}"/>'
        for r in range(3)
        for c in range(4)
    )
    return (
        '<g class="float"><rect x="700" y="20" width="160" height="142" rx="8" fill="#fff" fill-opacity=".95"/>'
        '<text x="716" y="44" font-size="14" font-weight="800" fill="#27415f">DEVIS</text>'
        f"{rows}"
        '<rect x="712" y="146" width="136" height="12" rx="3" fill="#2e9e6b" fill-opacity=".25"/>'
        '<text x="720" y="156" font-size="9" font-weight="800" fill="#1d3b2c">TOTAL TTC</text></g>'
        '<g class="float" style="animation-delay:-1.5s"><rect x="906" y="30" width="116" height="128" rx="10" fill="#27415f"/>'
        '<rect x="916" y="40" width="96" height="30" rx="4" fill="#bfe6c9"/>'
        '<text x="1006" y="62" text-anchor="end" font-size="16" font-weight="700" fill="#1d3b2c">19,25 %</text>'
        f"{keys}</g>"
        '<g transform="rotate(35 1060 100)"><rect x="1040" y="90" width="90" height="12" rx="3" fill="#f2c200"/>'
        '<polygon points="1130,90 1146,96 1130,102" fill="#e8c9a0"/><rect x="1040" y="90" width="14" height="12" fill="#e57373"/></g>'
        '<g transform="rotate(-14 1110 140)"><circle cx="1110" cy="140" r="26" fill="none" stroke="#2e9e6b" stroke-width="3"/>'
        '<text x="1110" y="144" text-anchor="middle" font-size="10" font-weight="800" fill="#2e9e6b">ACCEPTÉ</text></g>'
    )


def _motif_marches() -> str:
    bars = "".join(
        f'<rect class="bar" x="{818 + k * 20}" y="{112 - bh}" width="13" height="{bh}" rx="2" fill="#2a7de1" '
        f'style="animation-delay:{k * 0.25:.2f}s"/>'
        for k, bh in enumerate([20, 34, 28, 48, 62])
    )
    return (
        _card(640, 24, 140, 96)
        + '<path d="M654 102 C672 102 684 48 710 48 S748 102 766 102 Z" fill="#cfe3fb" stroke="#2a7de1" stroke-width="2"/>'
        '<path d="M718 102 L718 54 C736 58 750 102 766 102 Z" fill="#2a7de1" fill-opacity=".55"/>'
        '<text x="710" y="116" text-anchor="middle" font-size="9" font-weight="700" fill="#27415f">μ ± σ</text>'
        + _card(796, 24, 130, 96, -1.0)
        + bars
        + '<line x1="808" y1="112" x2="916" y2="112" stroke="#8aa0b8"/>'
        + _card(942, 24, 92, 110, -2.0)
        + '<circle cx="988" cy="74" r="27" fill="none" stroke="#dbe5f1" stroke-width="9"/>'
        '<circle class="gauge" cx="988" cy="74" r="27" fill="none" stroke="#2e9e6b" stroke-width="9" stroke-linecap="round" '
        'transform="rotate(-90 988 74)" style="--off:48"/>'
        '<text x="988" y="79" text-anchor="middle" font-size="14" font-weight="800" fill="#1d3b2c">%</text>'
        '<text x="988" y="124" text-anchor="middle" font-size="9" font-weight="700" fill="#27415f">P(gain)</text>'
        + _card(1050, 24, 130, 110, -3.0)
        + '<g transform="rotate(-12 1090 70)"><rect x="1072" y="50" width="36" height="36" rx="7" fill="#fff" stroke="#27415f" stroke-width="2"/>'
        '<circle cx="1082" cy="60" r="3" fill="#27415f"/><circle cx="1098" cy="60" r="3" fill="#27415f"/>'
        '<circle cx="1090" cy="68" r="3" fill="#27415f"/><circle cx="1082" cy="76" r="3" fill="#27415f"/><circle cx="1098" cy="76" r="3" fill="#27415f"/></g>'
        '<g transform="rotate(14 1140 80)"><rect x="1124" y="64" width="32" height="32" rx="7" fill="#fff" stroke="#27415f" stroke-width="2"/>'
        '<circle cx="1133" cy="73" r="3" fill="#27415f"/><circle cx="1147" cy="87" r="3" fill="#27415f"/></g>'
        '<polyline points="1064,122 1088,112 1106,116 1130,100 1166,88" fill="none" stroke="#2e9e6b" stroke-width="3" stroke-linecap="round"/>'
    )


def _motif_stock() -> str:
    out = []
    for row, (n, y, x0) in enumerate([(3, 146, 650), (2, 122, 674), (1, 98, 698)]):
        for k in range(n):
            x = x0 + k * 48
            out.append(f'<rect x="{x}" y="{y}" width="46" height="24" rx="8" fill="#d6cdbb" stroke="#8c8573"/>')
            out.append(f'<text x="{x + 23}" y="{y + 15}" text-anchor="middle" font-size="7.5" font-weight="800" fill="#6b4f2a">CIMENT</text>')
    for k in range(7):
        out.append(f'<line x1="830" y1="{148 + k * 3.2:.1f}" x2="930" y2="{148 + k * 3.2:.1f}" stroke="#8a6a4f" stroke-width="4" stroke-linecap="round"/>')
    out.append('<rect x="850" y="142" width="5" height="32" fill="#555"/><rect x="900" y="142" width="5" height="32" fill="#555"/>')
    for row, (n, y) in enumerate([(5, 158), (4, 148), (3, 138)]):
        for k in range(n):
            x = 960 + k * 24 + row * 12
            out.append(f'<rect x="{x}" y="{y}" width="22" height="10" fill="#b5543a" stroke="#7d3623"/>')
    out.append(
        '<rect x="1070" y="162" width="90" height="8" fill="#b98b52"/><rect x="1076" y="170" width="12" height="0" fill="#b98b52"/>'
        '<rect x="1078" y="128" width="38" height="34" fill="#c9a27a" stroke="#8c6a46"/><rect x="1120" y="140" width="34" height="22" fill="#d8b48a" stroke="#8c6a46"/>'
        '<g class="pulse"><polygon points="1150,40 1176,86 1124,86" fill="#f2c200" stroke="#7a5b00" stroke-width="2"/>'
        '<text x="1150" y="78" text-anchor="middle" font-size="22" font-weight="900" fill="#7a5b00">!</text></g>'
    )
    return "".join(out)


def _motif_depenses() -> str:
    out = []
    for x, n in [(670, 3), (716, 6), (762, 2)]:
        for k in range(n):
            out.append(f'<ellipse cx="{x}" cy="{164 - k * 8}" rx="21" ry="8" fill="#f2c200" stroke="#a67c00" stroke-width="1.5"/>')
    out.append(
        '<g class="float"><rect x="816" y="104" width="96" height="50" rx="5" fill="#7ac47f" stroke="#2e7d32" stroke-width="2" transform="rotate(-6 864 129)"/>'
        '<circle cx="864" cy="129" r="14" fill="#e8f5e9" stroke="#2e7d32" transform="rotate(-6 864 129)"/>'
        '<text x="864" y="134" text-anchor="middle" font-size="12" font-weight="800" fill="#1b5e20" transform="rotate(-6 864 129)">F</text></g>'
    )
    out.append(_card(934, 24, 150, 112, -1.2))
    for k, bh in enumerate([30, 46, 38, 62, 52]):
        out.append(
            f'<rect class="bar" x="{950 + k * 26}" y="{116 - bh}" width="18" height="{bh}" rx="2" '
            f'fill="{"#e0603b" if k == 3 else "#2e9e6b"}" style="animation-delay:{k * 0.2:.1f}s"/>'
        )
    out.append(
        '<line x1="944" y1="116" x2="1074" y2="116" stroke="#8aa0b8"/>'
        '<text x="1009" y="40" text-anchor="middle" font-size="10" font-weight="700" fill="#27415f">FCFA par mois</text>'
        '<g class="float" style="animation-delay:-2s"><path d="M1102 36 h70 v112 l-9 -7 l-9 7 l-9 -7 l-9 7 l-9 -7 l-9 7 l-8 -7 z" fill="#fff" fill-opacity=".95"/>'
        + "".join(f'<line x1="1112" y1="{54 + k * 14}" x2="1160" y2="{54 + k * 14}" stroke="#c8d3e0" stroke-width="3"/>' for k in range(6))
        + "</g>"
    )
    return "".join(out)


def _motif_rapports() -> str:
    return (
        _card(650, 24, 180, 112)
        + '<polyline points="666,116 696,94 726,102 760,66 790,74 818,44" fill="none" stroke="#2a7de1" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>'
        '<line x1="660" y1="122" x2="822" y2="122" stroke="#8aa0b8"/>'
        + _card(846, 24, 120, 112, -1.4)
        + _pie(906, 80, 38, [0.45, 0.30, 0.25], ["#2a7de1", "#2e9e6b", "#f08a24"])
        + '<g class="float" style="animation-delay:-2.4s"><rect x="990" y="36" width="82" height="108" rx="6" fill="#fff" fill-opacity=".96"/>'
        '<rect x="1002" y="50" width="36" height="36" rx="4" fill="#1d6f42"/>'
        '<text x="1020" y="76" text-anchor="middle" font-size="22" font-weight="900" fill="#fff">X</text>'
        + "".join(f'<line x1="1002" y1="{100 + k * 10}" x2="1060" y2="{100 + k * 10}" stroke="#c8d3e0" stroke-width="3"/>' for k in range(4))
        + '</g><g class="float" style="animation-delay:-0.6s"><rect x="1092" y="48" width="78" height="100" rx="6" fill="#fff" fill-opacity=".96"/>'
        '<rect x="1104" y="60" width="38" height="24" rx="4" fill="#c0392b"/>'
        '<text x="1123" y="77" text-anchor="middle" font-size="12" font-weight="900" fill="#fff">PDF</text>'
        + "".join(f'<line x1="1104" y1="{98 + k * 10}" x2="1158" y2="{98 + k * 10}" stroke="#c8d3e0" stroke-width="3"/>' for k in range(4))
        + "</g>"
    )


def _motif_parametres() -> str:
    return (
        _gear(760, 96, 48, 10, "#c9d1da")
        + _gear(850, 68, 32, 8, "#f08a24", rev=True)
        + _gear(884, 136, 26, 7, "#2a7de1")
        + '<g class="float"><rect x="990" y="82" width="64" height="52" rx="8" fill="#f2c200" stroke="#7a5b00" stroke-width="2"/>'
        '<path d="M1002 82 v-16 a20 20 0 0 1 40 0 v16" fill="none" stroke="#6b7178" stroke-width="7"/>'
        '<circle cx="1022" cy="106" r="6" fill="#7a5b00"/><rect x="1020" y="108" width="4" height="14" fill="#7a5b00"/></g>'
        '<g class="float" style="animation-delay:-1.8s"><path d="M1118 40 l44 14 v34 c0 26 -20 40 -44 52 c-24 -12 -44 -26 -44 -52 v-34 z" fill="#2e9e6b" stroke="#1d3b2c" stroke-width="2"/>'
        '<path d="M1098 94 l14 14 l26 -30" fill="none" stroke="#fff" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/></g>'
    )


BANNER_THEMES = {
    "chantiers": ("Chantiers", "Suivi des chantiers en cours, budgets et avancement", ("#1f4e79", "#4f93cf", "#f6d9a8"), _motif_chantiers),
    "intervenants": ("Intervenants", "Équipes par chantier : ingénieur, techniciens, ouvriers, contrôle", ("#14506b", "#3a9aa8", "#f2dcae"), _motif_equipe),
    "clients": ("Clients", "Maîtres d'ouvrage, contacts et contrats", ("#3b3f7a", "#7b86c9", "#f2d9c4"), _motif_clients),
    "devis": ("Devis", "Chiffrage, TVA et suivi des offres", ("#1d4b3f", "#4aa38a", "#f3e3b3"), _motif_devis),
    "marches": ("Marchés BTP", "Prix, concurrence et probabilité de gain", ("#1f4e79", "#4f93cf", "#f6d9a8"), _motif_marches),
    "stock": ("Stock", "Matériaux, entrées et sorties, alertes de seuil", ("#6b3a1f", "#c98a4b", "#f6d9a8"), _motif_stock),
    "personnel": ("Personnel", "Employés, pointages et coût de main-d'œuvre", ("#7a3d12", "#e08a3c", "#f9dca8"), lambda: _motif_equipe(True)),
    "depenses": ("Dépenses", "Dépenses par chantier et par catégorie", ("#15522f", "#4fae6b", "#efe3a8"), _motif_depenses),
    "rapports": ("Rapports", "Synthèses par chantier et exports Excel", ("#2b3a55", "#6b86b3", "#e3d9c3"), _motif_rapports),
    "parametres": ("Paramètres", "Utilisateurs, sécurité et sauvegarde", ("#2f3640", "#6c7a89", "#d9d2c3"), _motif_parametres),
}
BANNER_KEYS = {
    "Chantiers": "chantiers",
    "Intervenants": "intervenants",
    "Clients": "clients",
    "Devis": "devis",
    "Marchés BTP": "marches",
    "Stock": "stock",
    "Personnel": "personnel",
    "Dépenses": "depenses",
    "Rapports": "rapports",
    "Paramètres": "parametres",
}


def banner_html(theme: str) -> str:
    """Bandeau animé (1200 x 200) propre à une page : décor du thème + engin de chantier qui passe."""
    title, subtitle, (c1, c2, c3), motif = BANNER_THEMES[theme]
    idx = list(BANNER_THEMES).index(theme)
    if idx % 2 == 0:
        vehicles = _truck_svg("#1f6fd1", "#174f98", 1, "t1", 194, 0.6) + _machine_svg("roller", -1, "t4", 194, 0.6)
    else:
        vehicles = _machine_svg("backhoe", 1, "t1", 194, 0.6) + _truck_svg("#f08a24", "#c26a14", -1, "t2", 194, 0.6)
    svg = f"""
<svg viewBox="0 0 1200 200" preserveAspectRatio="xMidYMax slice" role="img" aria-label="{html.escape(title)}">
<defs><linearGradient id="sk" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="{c1}"/><stop offset=".62" stop-color="{c2}"/><stop offset="1" stop-color="{c3}"/></linearGradient></defs>
<rect width="1200" height="200" fill="url(#sk)"/>
<g class="cloud"><ellipse cx="0" cy="40" rx="42" ry="12" fill="#fff" opacity=".45"/><ellipse cx="24" cy="33" rx="28" ry="11" fill="#fff" opacity=".45"/></g>
<g class="cloud" style="animation-delay:-28s"><ellipse cx="0" cy="92" rx="34" ry="10" fill="#fff" opacity=".35"/></g>
<rect y="170" width="1200" height="30" fill="#3a3f47"/><rect y="170" width="1200" height="6" fill="#9aa0a6"/>
<line class="road" x1="0" y1="187" x2="1200" y2="187" stroke="#f2f2f2" stroke-width="2"/>
{motif()}
<text x="36" y="66" font-size="31" font-weight="800" fill="#fff" stroke="#14202e" stroke-opacity=".35" stroke-width="3" paint-order="stroke">{html.escape(title)}</text>
<text x="36" y="92" font-size="14" fill="#fff" stroke="#14202e" stroke-opacity=".35" stroke-width="2.5" paint-order="stroke">{html.escape(subtitle)}</text>
<rect x="36" y="106" width="178" height="22" rx="11" fill="#14202e" fill-opacity=".45"/>
<text x="125" y="121" text-anchor="middle" font-size="11.5" font-weight="800" fill="#fff">BIL-BTP CONSTRUCTION</text>
{vehicles}
</svg>"""
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<style>{HERO_CSS}</style></head><body><div class="wrap banner">{svg}</div></body></html>'
    )


def render_banner(theme: str) -> None:
    components.html(banner_html(theme), height=204)


# --------------------------------------------------------------------------- #
# Données d'exemple : clients, devis et dépenses liés aux 4 chantiers en cours
# --------------------------------------------------------------------------- #
DEMO_GESTION = {
    "Immeuble R+3 Bonamoussadi (démo)": {
        "client": ("Société Immobilière Horizon SARL", "+237 699 12 34 56", "contact@horizon-demo.cm", "Bonamoussadi, Douala"),
        "lignes": [
            ("Terrassement et fondations", "forfait", 1, 0.14),
            ("Gros œuvre béton armé (poteaux, poutres, dalles)", "m³", 320, 0.34),
            ("Maçonnerie et cloisons", "m²", 1850, 0.14),
            ("Plomberie sanitaire", "forfait", 1, 0.09),
            ("Électricité courants forts et faibles", "forfait", 1, 0.09),
            ("Menuiserie, carrelage et peinture", "m²", 1400, 0.20),
        ],
        "avenant": [("Étanchéité de la terrasse", "m²", 260, 0.6), ("Local technique supplémentaire", "forfait", 1, 0.4)],
    },
    "Villa duplex Bastos (démo)": {
        "client": ("M. et Mme Ateba (particuliers)", "+237 677 45 21 08", "famille.ateba@demo-mail.cm", "Bastos, Yaoundé"),
        "lignes": [
            ("Terrassement et fondations", "forfait", 1, 0.12),
            ("Élévation des murs en agglomérés", "m²", 640, 0.22),
            ("Dalles et charpente béton", "m³", 85, 0.18),
            ("Couverture et étanchéité", "m²", 240, 0.10),
            ("Plomberie et sanitaires", "forfait", 1, 0.08),
            ("Électricité", "forfait", 1, 0.08),
            ("Carrelage, menuiserie et peinture", "m²", 420, 0.17),
            ("Aménagements extérieurs et clôture", "forfait", 1, 0.05),
        ],
        "avenant": [("Piscine : terrassement et coque", "forfait", 1, 0.7), ("Portail motorisé", "u", 1, 0.3)],
    },
    "Pont de desserte Kribi (démo)": {
        "client": ("Délégation régionale des Travaux publics du Sud", "+237 233 46 11 22", "drtp-sud@demo-admin.cm", "Kribi"),
        "lignes": [
            ("Installation de chantier et études d'exécution", "forfait", 1, 0.06),
            ("Terrassement et remblais d'accès", "m³", 5200, 0.10),
            ("Fondations profondes (pieux forés)", "m", 420, 0.22),
            ("Culées et piles en béton armé", "m³", 780, 0.24),
            ("Tablier : poutres préfabriquées et dalle", "m²", 640, 0.22),
            ("Chaussée, étanchéité et garde-corps", "m", 85, 0.10),
            ("Signalisation et finitions", "forfait", 1, 0.06),
        ],
        "avenant": [("Enrochement de protection des berges", "m³", 900, 1.0)],
    },
    "Entrepôt logistique Bafoussam (démo)": {
        "client": ("Groupe Logistique Ouest SA", "+237 655 90 34 71", "direction@logistique-ouest-demo.cm", "Zone industrielle, Bafoussam"),
        "lignes": [
            ("Terrassement et plateforme", "m³", 4200, 0.12),
            ("Fondations et dallage industriel", "m²", 3200, 0.26),
            ("Charpente métallique", "t", 58, 0.22),
            ("Bardage et couverture", "m²", 3600, 0.16),
            ("Quais de chargement et portes sectionnelles", "u", 6, 0.09),
            ("Électricité et éclairage", "forfait", 1, 0.08),
            ("Voirie et parking", "m²", 2400, 0.07),
        ],
        "avenant": [("Extension du quai n°7", "forfait", 1, 0.6), ("Bureau d'exploitation préfabriqué", "u", 1, 0.4)],
    },
}
DEPENSES_TYPE = [
    ("Matériaux", 0.45, ["Ciment CPJ 42,5 et liants", "Fer à béton HA8 à HA16", "Sable, gravier et agglomérés"]),
    ("Main-d'œuvre", 0.25, ["Paie quinzaine : équipe gros œuvre", "Paie quinzaine : ferrailleurs et coffreurs", "Paie quinzaine : finitions et manœuvres"]),
    ("Location d'engins", 0.10, ["Location tractopelle Bil-BTP", "Location rouleau compresseur Bil-BTP", "Location camion benne Bil-BTP"]),
    ("Transport", 0.07, ["Transport de matériaux", "Livraison de béton prêt à l'emploi", "Évacuation des déblais"]),
    ("Sous-traitance", 0.10, ["Sous-traitance électricité", "Sous-traitance plomberie", "Sous-traitance étanchéité"]),
    ("Administratif", 0.03, ["Assurance chantier", "Frais d'études et plans", "Taxes et autorisations"]),
]
DATE_REFERENCE_DEMO = date(2026, 10, 15)


def seed_gestion_demo() -> tuple[int, int, int]:
    """Remplit clients, devis (avec lignes) et dépenses pour les chantiers d'exemple.

    Retourne (clients créés, devis créés, dépenses créées). Sans doublon si relancée.
    """
    conn = get_connection()
    n_cl = n_dv = n_dep = 0
    with _WRITE_LOCK:
        try:
            for idx, ch in enumerate(DEMO_CHANTIERS, start=1):
                g = DEMO_GESTION.get(ch["nom"])
                row = conn.execute(
                    "SELECT id, budget, avancement, date_debut, client_id FROM chantiers WHERE nom = ?", (ch["nom"],)
                ).fetchone()
                if not g or not row:
                    continue
                cid, budget, avancement, debut, client_id = row
                nom_cl, tel, mail, adresse = g["client"]
                found = conn.execute("SELECT id FROM clients WHERE nom = ?", (nom_cl,)).fetchone()
                if found:
                    client = found[0]
                else:
                    client = conn.execute(
                        "INSERT INTO clients (nom, telephone, email, adresse) VALUES (?,?,?,?)", (nom_cl, tel, mail, adresse)
                    ).lastrowid
                    n_cl += 1
                if not client_id:
                    conn.execute("UPDATE chantiers SET client_id = ? WHERE id = ?", (client, cid))

                total_ht = round(budget / (1 + TVA_DEFAUT / 100) / 1000) * 1000
                for numero, lignes, montant, statut, jour in (
                    (f"DV-DEMO-{idx:02d}", g["lignes"], total_ht, "Accepté", debut),
                    (f"DV-DEMO-{idx:02d}-A", g["avenant"], total_ht * 0.06, "Envoyé", "2026-09-20"),
                ):
                    if conn.execute("SELECT 1 FROM devis WHERE numero = ?", (numero,)).fetchone():
                        continue
                    did = conn.execute(
                        "INSERT INTO devis (numero, client_id, chantier_id, date, statut, tva) VALUES (?,?,?,?,?,?)",
                        (numero, client, cid, jour, statut, TVA_DEFAUT),
                    ).lastrowid
                    for designation, unite, qte, part in lignes:
                        conn.execute(
                            "INSERT INTO devis_lignes (devis_id, designation, quantite, unite, prix_unitaire) VALUES (?,?,?,?,?)",
                            (did, designation, qte, unite, round(montant * part / qte)),
                        )
                    n_dv += 1

                cle = f"depenses_demo_{cid}"
                if not conn.execute("SELECT 1 FROM meta WHERE key = ?", (cle,)).fetchone():
                    depense_totale = budget * avancement / 100 * 0.92
                    d0 = date.fromisoformat(debut)
                    duree = (DATE_REFERENCE_DEMO - d0).days
                    for c_idx, (categorie, poids, descriptions) in enumerate(DEPENSES_TYPE):
                        for j, (description, part) in enumerate(zip(descriptions, (0.3, 0.3, 0.4))):
                            fraction = max(0.05, (j + 1) / 3 - c_idx * 0.04)
                            jour = (d0 + timedelta(days=int(duree * fraction))).isoformat()
                            montant = round(depense_totale * poids * part / 1000) * 1000
                            conn.execute(
                                "INSERT INTO depenses (chantier_id, categorie, description, montant, date) VALUES (?,?,?,?,?)",
                                (cid, categorie, description, montant, jour),
                            )
                            n_dep += 1
                    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, '1')", (cle,))
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            log.exception("Remplissage des données d'exemple annulé")
            st.error(f"Opération annulée : {exc}")
            return 0, 0, 0
    return n_cl, n_dv, n_dep


# --------------------------------------------------------------------------- #
# Données d'exemple : stock de matériaux avec entrées et sorties vers les chantiers
# --------------------------------------------------------------------------- #
# (nom, unité, prix unitaire, seuil d'alerte, total des entrées, part sortie vers les chantiers)
# Les 8 premiers noms sont ceux des relevés de prix de l'onglet « Marchés BTP » (comparaison stock / marché).
DEMO_STOCK = [
    ("Ciment CPJ 42,5 (sac 50 kg)", "sac", 6500, 200, 4200, 0.96),
    ("Fer à béton HA12 (barre 12 m)", "u", 8400, 150, 2600, 0.82),
    ("Fer à béton HA8 (barre 12 m)", "u", 4000, 150, 2200, 0.80),
    ("Sable de rivière", "m³", 12500, 40, 520, 0.88),
    ("Gravier 15/25", "m³", 26000, 30, 380, 0.70),
    ("Parpaing 15 cm", "u", 540, 2000, 38000, 0.75),
    ("Tube PVC Ø100 (6 m)", "u", 9800, 30, 240, 0.95),
    ("Peinture vinylique (seau 20 L)", "u", 39000, 10, 90, 0.55),
    ("Bois de coffrage (planche)", "u", 3500, 100, 1500, 0.72),
    ("Fil de ligature", "kg", 1200, 60, 700, 0.78),
    ("Carreaux 40x40", "m²", 6500, 100, 2400, 0.45),
    ("Câble électrique 2,5 mm² (rouleau)", "u", 24000, 8, 60, 0.70),
    ("Tôle bac alu (feuille)", "u", 11000, 40, 420, 0.50),
    ("Gasoil", "L", 750, 300, 6000, 0.90),
]
DATES_ENTREES_DEMO = ("2026-06-05", "2026-09-02")
DATES_SORTIES_DEMO = ("2026-07-14", "2026-08-19", "2026-09-23", "2026-10-12")
PART_SORTIES_CHANTIERS = (0.35, 0.20, 0.25, 0.20)


def seed_stock_demo() -> tuple[int, int]:
    """Crée 14 matériaux avec leurs mouvements (entrées fournisseurs, sorties vers les 4 chantiers).

    La quantité en stock est exactement la somme des entrées moins les sorties. Retourne
    (matériaux créés, mouvements créés) ; un matériau déjà présent n'est pas modifié.
    """
    conn = get_connection()
    n_mat = n_mvt = 0
    with _WRITE_LOCK:
        try:
            chantiers = []
            for ch in DEMO_CHANTIERS:
                row = conn.execute("SELECT id FROM chantiers WHERE nom = ?", (ch["nom"],)).fetchone()
                chantiers.append(row[0] if row else None)
            for nom, unite, prix, seuil, entrees, part_sortie in DEMO_STOCK:
                if conn.execute("SELECT 1 FROM materiaux WHERE nom = ?", (nom,)).fetchone():
                    continue
                e1 = round(entrees * 0.6)
                mouvements = [("Entrée", None, e1, DATES_ENTREES_DEMO[0], "Livraison fournisseur (exemple)"),
                              ("Entrée", None, entrees - e1, DATES_ENTREES_DEMO[1], "Livraison fournisseur (exemple)")]
                a_sortir = round(entrees * part_sortie)
                reste = a_sortir
                for k, (cid, part, jour) in enumerate(zip(chantiers, PART_SORTIES_CHANTIERS, DATES_SORTIES_DEMO)):
                    qte = reste if k == len(chantiers) - 1 else round(a_sortir * part)
                    reste -= qte
                    if qte > 0:
                        mouvements.append(("Sortie", cid, qte, jour, "Sortie vers chantier (exemple)"))
                quantite = entrees - a_sortir
                mid = conn.execute(
                    "INSERT INTO materiaux (nom, unite, quantite, seuil_alerte, prix_unitaire) VALUES (?,?,?,?,?)",
                    (nom, unite, quantite, seuil, prix),
                ).lastrowid
                n_mat += 1
                for type_mvt, cid, qte, jour, note in mouvements:
                    conn.execute(
                        "INSERT INTO mouvements_stock (materiau_id, chantier_id, type, quantite, date, note) VALUES (?,?,?,?,?,?)",
                        (mid, cid, type_mvt, qte, jour, note),
                    )
                    n_mvt += 1
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            log.exception("Remplissage du stock d'exemple annulé")
            st.error(f"Opération annulée : {exc}")
            return 0, 0
    return n_mat, n_mvt


def ensure_demo_data() -> None:
    """Au premier lancement : 4 chantiers « En cours » avec équipes, puis clients, devis et dépenses (une seule fois)."""
    if st.session_state.get("_demo_checked"):
        return
    st.session_state["_demo_checked"] = True
    try:
        ensure_schema()
        conn = get_connection()

        def deja_fait(cle: str) -> bool:
            return conn.execute("SELECT 1 FROM meta WHERE key = ?", (cle,)).fetchone() is not None

        def nb_chantiers(demo_seulement: bool) -> int:
            sql = "SELECT COUNT(*) FROM chantiers" + (" WHERE nom LIKE '%(démo)%'" if demo_seulement else "")
            return conn.execute(sql).fetchone()[0]

        if not deja_fait("equipes_demo_v2"):
            if nb_chantiers(False) == 0 or nb_chantiers(True) > 0:  # base vide, ou exemples à compléter
                seed_intervenants_demo()
            db_write("INSERT OR REPLACE INTO meta (key, value) VALUES ('equipes_demo_v2', '1')")
        if not deja_fait("gestion_demo_v1"):
            if nb_chantiers(True) > 0:
                seed_gestion_demo()
            db_write("INSERT OR REPLACE INTO meta (key, value) VALUES ('gestion_demo_v1', '1')")
        if not deja_fait("stock_demo_v1"):
            if conn.execute("SELECT COUNT(*) FROM materiaux").fetchone()[0] == 0:  # ne touche pas à un stock existant
                seed_stock_demo()
            db_write("INSERT OR REPLACE INTO meta (key, value) VALUES ('stock_demo_v1', '1')")
    except sqlite3.Error:
        log.exception("Création des données d'exemple impossible")  # ne jamais bloquer l'application


def hero_data() -> tuple[list[tuple[str, int]], float | None]:
    """Chantiers en cours (nom, avancement) et meilleure probabilité de gain, pour la scène du tableau de bord."""
    df = query("SELECT nom, avancement FROM chantiers WHERE statut = 'En cours' ORDER BY id LIMIT 6")
    items = [(str(r["nom"]).replace(" (démo)", ""), int(r["avancement"])) for _, r in df.iterrows()]
    proba = None
    try:
        ranking, _ = rank_opportunities(competitor_model(0.92, 0.07), 8.0, 100.0)
        if not ranking.empty and float(ranking.iloc[0]["Gain espéré"]) > 0:
            proba = float(ranking.iloc[0]["Proba de gain"])
    except Exception:  # noqa: BLE001 - la scène ne doit jamais bloquer la page
        log.exception("Calcul de la probabilité pour la scène")
    return items, proba


# --------------------------------------------------------------------------- #
# Tableau de bord
# --------------------------------------------------------------------------- #
def page_dashboard() -> None:
    st.header("📊 Tableau de bord")
    scene_chantiers, scene_proba = hero_data()
    render_hero(scene_chantiers, scene_proba)
    chantiers = query("SELECT * FROM chantiers")
    depenses = query("SELECT * FROM depenses")

    en_cours = int((chantiers["statut"] == "En cours").sum()) if not chantiers.empty else 0
    budget_total = float(chantiers["budget"].sum()) if not chantiers.empty else 0.0
    depense_total = float(depenses["montant"].sum()) if not depenses.empty else 0.0
    reste = budget_total - depense_total

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Chantiers en cours", en_cours)
    c2.metric("Budget total", fcfa(budget_total))
    c3.metric("Dépenses", fcfa(depense_total))
    c4.metric("Reste", fcfa(reste), delta=None if budget_total == 0 else f"{reste / budget_total:.0%}")

    st.subheader("Alertes")
    today = date.today().isoformat()
    retard = query(
        "SELECT nom, date_fin_prevue, avancement FROM chantiers "
        "WHERE date_fin_prevue < ? AND statut IN ('Planifié','En cours','Suspendu')",
        (today,),
    )
    stock_bas = query(
        "SELECT nom, quantite, seuil_alerte, unite FROM materiaux WHERE quantite <= seuil_alerte"
    )
    depassements = query(
        """
        SELECT c.nom AS nom, c.budget AS budget, COALESCE(SUM(d.montant), 0) AS depense
        FROM chantiers c LEFT JOIN depenses d ON d.chantier_id = c.id
        GROUP BY c.id HAVING c.budget > 0 AND depense > c.budget
        """
    )
    if retard.empty and stock_bas.empty and depassements.empty:
        st.success("Aucune alerte : tout est sous contrôle.")
    if not retard.empty:
        st.warning(f"{len(retard)} chantier(s) en retard sur la date prévue.")
        st.dataframe(
            retard.rename(columns={"nom": "Chantier", "date_fin_prevue": "Fin prévue", "avancement": "Avancement %"}),
            hide_index=True,
            use_container_width=True,
        )
    if not stock_bas.empty:
        st.warning(f"{len(stock_bas)} matériau(x) sous le seuil d'alerte.")
        st.dataframe(
            stock_bas.rename(columns={"nom": "Matériau", "quantite": "Quantité", "seuil_alerte": "Seuil", "unite": "Unité"}),
            hide_index=True,
            use_container_width=True,
        )
    if not depassements.empty:
        st.error(f"{len(depassements)} chantier(s) dépassent leur budget.")
        st.dataframe(
            depassements.rename(columns={"nom": "Chantier", "budget": "Budget", "depense": "Dépensé"}),
            hide_index=True,
            use_container_width=True,
        )

    st.subheader("Budget et dépenses par chantier")
    compare = query(
        """
        SELECT c.nom AS Chantier, c.budget AS Budget, COALESCE(SUM(d.montant), 0) AS Dépenses
        FROM chantiers c LEFT JOIN depenses d ON d.chantier_id = c.id GROUP BY c.id
        """
    )
    if compare.empty:
        st.info("Ajoutez un chantier pour voir les graphiques.")
    else:
        st.bar_chart(compare.set_index("Chantier"))

    left, right = st.columns(2)
    with left:
        st.subheader("Dépenses par catégorie")
        if depenses.empty:
            st.caption("Aucune dépense enregistrée.")
        else:
            st.bar_chart(depenses.groupby("categorie")["montant"].sum())
    with right:
        st.subheader("Dépenses par mois")
        if depenses.empty:
            st.caption("Aucune dépense enregistrée.")
        else:
            monthly = depenses.assign(mois=depenses["date"].str[:7]).groupby("mois")["montant"].sum()
            st.line_chart(monthly)


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #
def validate_client(nom: str, telephone: str, email: str) -> bool:
    if not nom.strip():
        st.error("Le nom du client est obligatoire.")
        return False
    if not valid_phone(telephone):
        st.error("Numéro de téléphone invalide.")
        return False
    if not valid_email(email):
        st.error("Adresse e-mail invalide.")
        return False
    return True


def page_clients() -> None:
    st.header("👥 Clients")
    tab_list, tab_add, tab_edit = st.tabs(["Liste", "Ajouter", "Modifier / supprimer"])

    with tab_list:
        search = st.text_input("Rechercher (nom, e-mail, téléphone)", key="client_search")
        df = query("SELECT id, nom, telephone, email, adresse FROM clients ORDER BY nom")
        if search and not df.empty:
            mask = df.apply(lambda r: search.lower() in " ".join(map(str, r.values)).lower(), axis=1)
            df = df[mask]
        st.dataframe(df, hide_index=True, use_container_width=True)
        st.caption(f"{len(df)} client(s)")

    with tab_add:
        if not can_edit():
            read_only_notice()
        else:
            with st.form("client_add", clear_on_submit=True):
                nom = st.text_input("Nom / raison sociale *")
                telephone = st.text_input("Téléphone")
                email = st.text_input("E-mail")
                adresse = st.text_area("Adresse")
                if st.form_submit_button("Enregistrer", type="primary"):
                    if validate_client(nom, telephone, email):
                        if db_write(
                            "INSERT INTO clients (nom, telephone, email, adresse) VALUES (?,?,?,?)",
                            (nom.strip(), telephone.strip(), email.strip(), adresse.strip()),
                        ) is not None:
                            flash("Client ajouté.")
                            st.rerun()

    with tab_edit:
        clients = choice_map("SELECT id, nom FROM clients ORDER BY nom")
        if not clients:
            st.info("Aucun client à modifier.")
        elif not can_edit():
            read_only_notice()
        else:
            cid = st.selectbox("Client", list(clients), format_func=clients.get, key="client_sel")
            row = query("SELECT * FROM clients WHERE id = ?", (cid,)).iloc[0]
            with st.form(f"client_edit_{cid}"):
                nom = st.text_input("Nom *", value=row["nom"] or "")
                telephone = st.text_input("Téléphone", value=row["telephone"] or "")
                email = st.text_input("E-mail", value=row["email"] or "")
                adresse = st.text_area("Adresse", value=row["adresse"] or "")
                if st.form_submit_button("Mettre à jour", type="primary"):
                    if validate_client(nom, telephone, email):
                        if db_write(
                            "UPDATE clients SET nom=?, telephone=?, email=?, adresse=? WHERE id=?",
                            (nom.strip(), telephone.strip(), email.strip(), adresse.strip(), cid),
                        ) is not None:
                            flash("Client mis à jour.")
                            st.rerun()
            if is_admin():
                st.divider()
                if confirm_delete(f"client_{cid}") and st.button("Supprimer ce client", key=f"del_client_{cid}"):
                    if db_write("DELETE FROM clients WHERE id = ?", (cid,)) is not None:
                        flash("Client supprimé.")
                        st.rerun()


# --------------------------------------------------------------------------- #
# Chantiers
# --------------------------------------------------------------------------- #
def chantier_form(prefix: str, row: pd.Series | None, clients: dict[int, str]) -> dict:
    """Affiche les champs d'un chantier et retourne les valeurs saisies."""
    client_ids = [0] + list(clients)
    labels = {0: "— Aucun —", **clients}
    current_client = int(row["client_id"]) if row is not None and pd.notna(row["client_id"]) else 0
    nom = st.text_input("Nom du chantier *", value=row["nom"] if row is not None else "", key=f"{prefix}_nom")
    client_id = st.selectbox(
        "Client",
        client_ids,
        index=client_ids.index(current_client) if current_client in client_ids else 0,
        format_func=labels.get,
        key=f"{prefix}_client",
    )
    localisation = st.text_input(
        "Localisation", value=(row["localisation"] or "") if row is not None else "", key=f"{prefix}_loc"
    )
    col1, col2 = st.columns(2)
    debut = col1.date_input(
        "Date de début", value=to_date(row["date_debut"]) if row is not None else date.today(), key=f"{prefix}_debut"
    )
    fin = col2.date_input(
        "Fin prévue", value=to_date(row["date_fin_prevue"]) if row is not None else date.today(), key=f"{prefix}_fin"
    )
    col3, col4 = st.columns(2)
    budget = col3.number_input(
        f"Budget ({DEVISE})",
        min_value=0.0,
        step=100000.0,
        format="%.0f",
        value=float(row["budget"]) if row is not None else 0.0,
        key=f"{prefix}_budget",
    )
    statut = col4.selectbox(
        "Statut",
        STATUTS_CHANTIER,
        index=STATUTS_CHANTIER.index(row["statut"]) if row is not None and row["statut"] in STATUTS_CHANTIER else 0,
        key=f"{prefix}_statut",
    )
    avancement = st.slider(
        "Avancement (%)", 0, 100, int(row["avancement"]) if row is not None else 0, key=f"{prefix}_avanc"
    )
    notes = st.text_area("Notes", value=(row["notes"] or "") if row is not None else "", key=f"{prefix}_notes")

    return {
        "nom": nom.strip(),
        "client_id": client_id or None,
        "localisation": localisation.strip(),
        "date_debut": debut.isoformat(),
        "date_fin_prevue": fin.isoformat(),
        "budget": budget,
        "statut": statut,
        "avancement": 100 if statut == "Terminé" else avancement,
        "notes": notes.strip(),
    }


def validate_chantier(data: dict) -> bool:
    if not data["nom"]:
        st.error("Le nom du chantier est obligatoire.")
        return False
    if data["date_fin_prevue"] < data["date_debut"]:
        st.error("La date de fin prévue ne peut pas précéder la date de début.")
        return False
    return True


def page_chantiers() -> None:
    st.header("🏗️ Chantiers")
    clients = choice_map("SELECT id, nom FROM clients ORDER BY nom")
    tab_list, tab_add, tab_edit, tab_detail = st.tabs(["Liste", "Nouveau", "Modifier / supprimer", "Fiche détaillée"])

    with tab_list:
        col1, col2 = st.columns([2, 1])
        search = col1.text_input("Rechercher un chantier", key="chantier_search")
        statuts = col2.multiselect("Statut", STATUTS_CHANTIER, default=[], key="chantier_statuts")
        df = query(
            """
            SELECT ch.id, ch.nom, cl.nom AS client, ch.localisation, ch.date_debut,
                   ch.date_fin_prevue, ch.budget, ch.statut, ch.avancement
            FROM chantiers ch LEFT JOIN clients cl ON cl.id = ch.client_id
            ORDER BY ch.date_debut DESC
            """
        )
        if not df.empty:
            if search:
                df = df[df["nom"].str.contains(search, case=False, na=False)]
            if statuts:
                df = df[df["statut"].isin(statuts)]
        st.dataframe(
            df,
            hide_index=True,
            use_container_width=True,
            column_config={
                "budget": st.column_config.NumberColumn("Budget", format="%d"),
                "avancement": st.column_config.ProgressColumn("Avancement", min_value=0, max_value=100, format="%d%%"),
            },
        )
        st.caption(f"{len(df)} chantier(s)")

    with tab_add:
        if not can_edit():
            read_only_notice()
        else:
            data = chantier_form("add_ch", None, clients)
            if st.button("Créer le chantier", type="primary", key="add_ch_btn") and validate_chantier(data):
                if db_write(
                    "INSERT INTO chantiers (nom, client_id, localisation, date_debut, date_fin_prevue, "
                    "budget, statut, avancement, notes) VALUES (:nom,:client_id,:localisation,:date_debut,"
                    ":date_fin_prevue,:budget,:statut,:avancement,:notes)",
                    data,  # type: ignore[arg-type]
                ) is not None:
                    flash("Chantier créé.")
                    st.rerun()

    with tab_edit:
        chantiers = choice_map("SELECT id, nom FROM chantiers ORDER BY nom")
        if not chantiers:
            st.info("Aucun chantier à modifier.")
        elif not can_edit():
            read_only_notice()
        else:
            cid = st.selectbox("Chantier", list(chantiers), format_func=chantiers.get, key="chantier_sel")
            row = query("SELECT * FROM chantiers WHERE id = ?", (cid,)).iloc[0]
            prefix = f"edit_ch_{cid}"
            data = chantier_form(prefix, row, clients)
            if st.button("Enregistrer les modifications", type="primary", key=f"{prefix}_btn") and validate_chantier(data):
                data["id"] = cid
                if db_write(
                    "UPDATE chantiers SET nom=:nom, client_id=:client_id, localisation=:localisation, "
                    "date_debut=:date_debut, date_fin_prevue=:date_fin_prevue, budget=:budget, "
                    "statut=:statut, avancement=:avancement, notes=:notes WHERE id=:id",
                    data,  # type: ignore[arg-type]
                ) is not None:
                    flash("Chantier mis à jour.")
                    st.rerun()
            if is_admin():
                st.divider()
                if confirm_delete(f"chantier_{cid}") and st.button("Supprimer ce chantier", key=f"del_ch_{cid}"):
                    if db_write("DELETE FROM chantiers WHERE id = ?", (cid,)) is not None:
                        flash("Chantier supprimé.")
                        st.rerun()

    with tab_detail:
        chantiers = choice_map("SELECT id, nom FROM chantiers ORDER BY nom")
        if not chantiers:
            st.info("Aucun chantier.")
        else:
            cid = st.selectbox("Chantier", list(chantiers), format_func=chantiers.get, key="chantier_detail_sel")
            show_chantier_detail(cid)


def show_chantier_detail(cid: int) -> None:
    ch = query(
        "SELECT ch.*, cl.nom AS client FROM chantiers ch LEFT JOIN clients cl ON cl.id = ch.client_id "
        "WHERE ch.id = ?",
        (cid,),
    ).iloc[0]
    depenses = query("SELECT * FROM depenses WHERE chantier_id = ? ORDER BY date DESC", (cid,))
    pointages = query(
        "SELECT p.date, e.nom, e.taux_journalier, p.heures FROM pointages p "
        "JOIN employes e ON e.id = p.employe_id WHERE p.chantier_id = ?",
        (cid,),
    )
    total_dep = float(depenses["montant"].sum()) if not depenses.empty else 0.0
    cout_mo = (
        float((pointages["heures"] / HEURES_PAR_JOUR * pointages["taux_journalier"]).sum())
        if not pointages.empty
        else 0.0
    )
    budget = float(ch["budget"])

    st.subheader(ch["nom"])
    st.caption(f"Client : {ch['client'] or '—'} · {ch['localisation'] or 'Lieu non précisé'}")
    st.progress(int(ch["avancement"]) / 100, text=f"Avancement : {int(ch['avancement'])} %")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Budget", fcfa(budget))
    c2.metric("Dépenses", fcfa(total_dep))
    c3.metric("Main-d'œuvre (pointages)", fcfa(cout_mo))
    c4.metric("Reste budget", fcfa(budget - total_dep))
    if budget > 0 and total_dep > budget:
        st.error("Le budget est dépassé.")
    st.write(f"**Statut :** {ch['statut']} · **Période :** {ch['date_debut']} → {ch['date_fin_prevue']}")
    if ch["notes"]:
        st.write(f"**Notes :** {ch['notes']}")

    team = team_frame(cid)
    st.write("**Intervenants du chantier**")
    if team.empty:
        st.caption("Aucun intervenant affecté. Utilisez la page « Intervenants ».")
    else:
        st.dataframe(team_display(team), hide_index=True, use_container_width=True)

    if not depenses.empty:
        st.write("**Dépenses du chantier**")
        st.dataframe(depenses.drop(columns=["chantier_id"]), hide_index=True, use_container_width=True)
        st.bar_chart(depenses.groupby("categorie")["montant"].sum())


# --------------------------------------------------------------------------- #
# Intervenants des chantiers (ingénieur, technicien, manœuvres, plombiers…)
# --------------------------------------------------------------------------- #
EQUIPE_TYPE = [
    ("Maître d'ouvrage", 1),
    ("Ingénieur", 1),
    ("Technicien", 1),
    ("Contrôleur technique", 1),
    ("Coffreur", 2),
    ("Ferrailleur", 2),
    ("Plombier", 2),
    ("Manœuvre", 4),
]
TAUX_FONCTION = {
    "Maître d'ouvrage": 0,
    "Ingénieur": 25000,
    "Technicien": 15000,
    "Contrôleur technique": 20000,
    "Coffreur": 9000,
    "Ferrailleur": 8000,
    "Plombier": 8000,
    "Manœuvre": 5000,
}
DEMO_CHANTIERS = [
    {
        "nom": "Immeuble R+3 Bonamoussadi (démo)",
        "localisation": "Douala",
        "debut": "2026-03-02",
        "fin": "2027-04-30",
        "budget": 180_000_000,
        "avancement": 45,
    },
    {
        "nom": "Villa duplex Bastos (démo)",
        "localisation": "Yaoundé",
        "debut": "2026-05-18",
        "fin": "2027-02-28",
        "budget": 65_000_000,
        "avancement": 60,
    },
    {
        "nom": "Pont de desserte Kribi (démo)",
        "localisation": "Kribi",
        "debut": "2026-01-12",
        "fin": "2027-06-30",
        "budget": 320_000_000,
        "avancement": 30,
    },
    {
        "nom": "Entrepôt logistique Bafoussam (démo)",
        "localisation": "Bafoussam",
        "debut": "2026-04-06",
        "fin": "2027-01-29",
        "budget": 95_000_000,
        "avancement": 70,
    },
]
PRENOMS = [
    "Jean", "Paul", "Marie", "Alain", "Brice", "Carine", "Didier", "Estelle", "Fabrice", "Grâce",
    "Hervé", "Irène", "Joël", "Kevin", "Landry", "Mireille", "Nadège", "Olivier", "Patrice", "Rodrigue",
]
NOMS = [
    "Mbarga", "Nkoulou", "Tchinda", "Fotso", "Essomba", "Kamga", "Ndongo", "Abena", "Mvondo", "Tagne",
    "Ngassa", "Owona", "Biyiha", "Djoumessi", "Fouda", "Atangana", "Njoya", "Kouam", "Eyenga", "Ewane",
]


def demo_person(i: int, used: set[str]) -> tuple[str, str]:
    """Nom unique et numéro de téléphone fictif (format camerounais) pour la personne n° i."""
    prenom = PRENOMS[i % len(PRENOMS)]
    k = (i * 3 + i // len(PRENOMS)) % len(NOMS)
    nom = f"{prenom} {NOMS[k]}"
    for _ in range(len(NOMS)):
        nom = f"{prenom} {NOMS[k]}"
        if nom not in used:
            break
        k = (k + 1) % len(NOMS)
    used.add(nom)
    tel = f"+237 6{70 + i % 30} {(i * 37) % 90 + 10:02d} {(i * 53) % 90 + 10:02d} {(i * 17) % 90 + 10:02d}"
    return nom, tel


def team_frame(cid: int) -> pd.DataFrame:
    """Équipe d'un chantier, triée par fonction, avec une colonne N°."""
    df = query(
        "SELECT a.id AS affectation_id, e.id AS employe_id, e.nom, a.fonction, e.telephone, e.taux_journalier "
        "FROM affectations a JOIN employes e ON e.id = a.employe_id WHERE a.chantier_id = ?",
        (cid,),
    )
    if df.empty:
        return df
    df["_ordre"] = df["fonction"].map({f: i for i, f in enumerate(FONCTIONS_CHANTIER)}).fillna(99)
    df = df.sort_values(["_ordre", "nom"]).drop(columns="_ordre").reset_index(drop=True)
    df.insert(0, "N°", range(1, len(df) + 1))
    return df


def team_display(team: pd.DataFrame) -> pd.DataFrame:
    return team.drop(columns=["affectation_id", "employe_id"]).rename(
        columns={
            "nom": "Nom",
            "fonction": "Fonction",
            "telephone": "Téléphone",
            "taux_journalier": f"Taux journalier ({DEVISE})",
        }
    )


def seed_intervenants_demo() -> tuple[int, int]:
    """Crée (ou complète) 4 chantiers « En cours » avec leur équipe type.

    Retourne (chantiers créés, intervenants ajoutés). Peut être relancée sans créer de doublons :
    seuls les postes manquants sont ajoutés.
    """
    conn = get_connection()
    created = added = 0
    with _WRITE_LOCK:
        try:
            used = {r[0] for r in conn.execute("SELECT nom FROM employes")}
            counter = conn.execute("SELECT COUNT(*) FROM employes").fetchone()[0]
            for ch in DEMO_CHANTIERS:
                row = conn.execute("SELECT id FROM chantiers WHERE nom = ?", (ch["nom"],)).fetchone()
                if row:
                    cid = row[0]
                else:
                    cur = conn.execute(
                        "INSERT INTO chantiers (nom, localisation, date_debut, date_fin_prevue, budget, "
                        "statut, avancement, notes) VALUES (?,?,?,?,?, 'En cours', ?, ?)",
                        (
                            ch["nom"],
                            ch["localisation"],
                            ch["debut"],
                            ch["fin"],
                            ch["budget"],
                            ch["avancement"],
                            "Données d'exemple : à remplacer par vos vrais chantiers et intervenants.",
                        ),
                    )
                    cid = cur.lastrowid
                    created += 1
                for fonction, effectif in EQUIPE_TYPE:
                    deja = conn.execute(
                        "SELECT COUNT(*) FROM affectations WHERE chantier_id = ? AND fonction = ?", (cid, fonction)
                    ).fetchone()[0]
                    for _ in range(max(effectif - deja, 0)):
                        nom, tel = demo_person(counter, used)
                        counter += 1
                        emp = conn.execute(
                            "INSERT INTO employes (nom, poste, telephone, taux_journalier, actif) VALUES (?,?,?,?,1)",
                            (nom, fonction, tel, TAUX_FONCTION[fonction]),
                        )
                        conn.execute(
                            "INSERT INTO affectations (chantier_id, employe_id, fonction) VALUES (?,?,?)",
                            (cid, emp.lastrowid, fonction),
                        )
                        added += 1
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            log.exception("Création des données d'exemple annulée")
            st.error(f"Opération annulée : {exc}")
            return 0, 0
    return created, added


def sheet_name(cid: int, nom: str) -> str:
    return re.sub(r"[\[\]:*?/\\]", "", f"{cid}-{nom}")[:31]


def show_fiche_intervenants() -> None:
    statuts = st.multiselect(
        "Statut des chantiers affichés", STATUTS_CHANTIER, default=["En cours"], key="int_statuts"
    )
    if not statuts:
        st.info("Choisissez au moins un statut.")
        return
    marks = ",".join("?" * len(statuts))
    chantiers = query(
        f"SELECT id, nom, localisation, statut, avancement FROM chantiers WHERE statut IN ({marks}) ORDER BY nom",
        tuple(statuts),
    )
    if chantiers.empty:
        st.info("Aucun chantier avec ce statut. Créez-en dans « Chantiers » ou chargez les données d'exemple.")
        return

    teams = {int(r["id"]): team_frame(int(r["id"])) for _, r in chantiers.iterrows()}
    rows = []
    for n, (_, r) in enumerate(chantiers.iterrows(), start=1):
        team = teams[int(r["id"])]
        counts = team["fonction"].value_counts() if not team.empty else pd.Series(dtype=int)
        row = {
            "N°": n,
            "Chantier": r["nom"],
            "Localisation": r["localisation"] or "—",
            "Avancement %": int(r["avancement"]),
        }
        for f in FONCTIONS_CHANTIER:
            row[f] = int(counts.get(f, 0))
        row["Effectif total"] = len(team)
        manquants = [f for f in FONCTIONS_CHANTIER if row[f] == 0]
        row["Postes manquants"] = ", ".join(manquants) if manquants else "—"
        rows.append(row)
    recap = pd.DataFrame(rows)

    c1, c2 = st.columns(2)
    c1.metric("Chantiers affichés", len(recap))
    c2.metric("Intervenants affectés", int(recap["Effectif total"].sum()))
    st.subheader("Récapitulatif par chantier")
    st.dataframe(recap, hide_index=True, use_container_width=True)
    incomplets = recap[recap["Postes manquants"] != "—"]
    if not incomplets.empty:
        st.warning(f"{len(incomplets)} chantier(s) avec au moins une fonction sans intervenant.")

    st.subheader("Fiche détaillée de chaque chantier")
    sheets: dict[str, pd.DataFrame] = {"Récapitulatif": recap}
    for idx, (_, r) in enumerate(chantiers.iterrows()):
        cid = int(r["id"])
        team = teams[cid]
        with st.expander(f"{r['nom']} — {r['localisation'] or 'lieu non précisé'} ({len(team)} intervenant(s))", expanded=idx == 0):
            if team.empty:
                st.caption("Aucun intervenant affecté à ce chantier.")
            else:
                shown = team_display(team)
                st.dataframe(shown, hide_index=True, use_container_width=True)
                sheets[sheet_name(cid, r["nom"])] = shown
    st.download_button(
        "⬇️ Télécharger la fiche des intervenants (Excel)",
        data=excel_bytes(sheets),
        file_name=f"intervenants_chantiers_{date.today().isoformat()}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def show_affectations_editor() -> None:
    chantiers = choice_map("SELECT id, nom FROM chantiers ORDER BY nom")
    if not chantiers:
        st.info("Créez d'abord un chantier.")
        return
    cid = st.selectbox("Chantier", list(chantiers), format_func=chantiers.get, key="aff_chantier")
    team = team_frame(cid)
    if team.empty:
        st.caption("Aucun intervenant affecté à ce chantier.")
    else:
        st.dataframe(team_display(team), hide_index=True, use_container_width=True)
    if not can_edit():
        read_only_notice()
        return

    col_a, col_b = st.columns(2)
    with col_a:
        st.write("**Affecter un employé existant**")
        dispo = choice_map(
            "SELECT id, nom || ' — ' || COALESCE(poste, '') FROM employes WHERE actif = 1 AND id NOT IN "
            f"(SELECT employe_id FROM affectations WHERE chantier_id = {int(cid)}) ORDER BY nom"
        )
        if not dispo:
            st.caption("Aucun employé disponible : créez un nouvel intervenant à droite.")
        else:
            with st.form(f"aff_add_{cid}", clear_on_submit=True):
                eid = st.selectbox("Employé", list(dispo), format_func=dispo.get, key=f"aff_emp_{cid}")
                fonction = st.selectbox("Fonction", FONCTIONS_CHANTIER, key=f"aff_fonc_{cid}")
                if st.form_submit_button("Affecter", type="primary"):
                    if db_write(
                        "INSERT INTO affectations (chantier_id, employe_id, fonction) VALUES (?,?,?)",
                        (cid, eid, fonction),
                    ) is not None:
                        flash("Intervenant affecté.")
                        st.rerun()
    with col_b:
        st.write("**Créer un nouvel intervenant**")
        with st.form(f"aff_new_{cid}", clear_on_submit=True):
            nom = st.text_input("Nom complet *", key=f"new_nom_{cid}")
            tel = st.text_input("Téléphone", key=f"new_tel_{cid}")
            taux = st.number_input(
                f"Taux journalier ({DEVISE})", min_value=0.0, step=500.0, format="%.0f", key=f"new_taux_{cid}"
            )
            fonction = st.selectbox("Fonction", FONCTIONS_CHANTIER, key=f"new_fonc_{cid}")
            if st.form_submit_button("Créer et affecter", type="primary"):
                if not nom.strip():
                    st.error("Le nom est obligatoire.")
                elif not valid_phone(tel):
                    st.error("Numéro de téléphone invalide.")
                else:
                    eid = db_write(
                        "INSERT INTO employes (nom, poste, telephone, taux_journalier) VALUES (?,?,?,?)",
                        (nom.strip(), fonction, tel.strip(), taux),
                    )
                    if eid is not None and db_write(
                        "INSERT INTO affectations (chantier_id, employe_id, fonction) VALUES (?,?,?)",
                        (cid, eid, fonction),
                    ) is not None:
                        flash("Intervenant créé et affecté.")
                        st.rerun()

    if not team.empty:
        st.divider()
        labels = {
            int(r["affectation_id"]): f"{int(r['N°'])} · {r['nom']} ({r['fonction']})" for _, r in team.iterrows()
        }
        aid = st.selectbox("Retirer un intervenant du chantier", list(labels), format_func=labels.get, key=f"aff_del_{cid}")
        if st.button("Retirer du chantier", key=f"aff_del_btn_{cid}"):
            if db_write("DELETE FROM affectations WHERE id = ?", (aid,)) is not None:
                flash("Intervenant retiré du chantier.")
                st.rerun()


def page_intervenants() -> None:
    st.header("🧑‍🔧 Intervenants des chantiers")
    st.caption("Ingénieur, technicien, manœuvres, plombiers, ferrailleurs et contrôleurs techniques par chantier.")
    tab_fiche, tab_aff, tab_demo = st.tabs(["Fiche des chantiers", "Affecter / retirer", "Données d'exemple"])
    with tab_fiche:
        show_fiche_intervenants()
    with tab_aff:
        show_affectations_editor()
    with tab_demo:
        st.write(
            "Crée **4 chantiers « En cours »** (Douala, Yaoundé, Kribi, Bafoussam) avec, pour chacun, "
            "1 maître d'ouvrage, 1 ingénieur, 1 technicien, 1 contrôleur technique, 2 coffreurs, "
            "2 ferrailleurs, 2 plombiers et 4 manœuvres (14 personnes par chantier). "
            "Ils sont aussi créés automatiquement au premier lancement."
        )
        st.caption(
            "Noms, numéros de téléphone et taux sont fictifs : remplacez-les par vos données réelles "
            "dans l'onglet « Affecter / retirer » ou dans « Personnel »."
        )
        if not can_edit():
            read_only_notice()
        elif st.button("Créer les chantiers et équipes d'exemple", type="primary", key="seed_int"):
            created, added = seed_intervenants_demo()
            if created or added:
                flash(f"{created} chantier(s) créé(s), {added} intervenant(s) ajouté(s).")
                st.rerun()
            else:
                st.info("Les chantiers d'exemple existent déjà avec leurs équipes complètes.")
        if can_edit():
            st.divider()
            st.write("**Clients, devis et dépenses** liés aux 4 chantiers (créés aussi automatiquement au premier lancement).")
            if st.button("Compléter clients, devis et dépenses", key="seed_gest"):
                n_cl, n_dv, n_dep = seed_gestion_demo()
                if n_cl or n_dv or n_dep:
                    flash(f"{n_cl} client(s), {n_dv} devis et {n_dep} dépense(s) ajoutés.")
                    st.rerun()
                else:
                    st.info("Clients, devis et dépenses d'exemple déjà présents.")


# --------------------------------------------------------------------------- #
# Marchés BTP : modèle de probabilité
# --------------------------------------------------------------------------- #
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def competitor_model(prior_mu: float, prior_sigma: float) -> dict:
    """Estime comment les concurrents proposent leurs prix (en % de l'estimation du maître d'ouvrage).

    Part d'une hypothèse de départ puis la corrige avec les offres réellement enregistrées :
    plus il y a d'observations, plus l'historique pèse.
    """
    df = query(
        "SELECT c.montant AS montant, a.estimation AS estimation FROM concurrents_offres c "
        "JOIN appels_offres a ON a.id = c.appel_id WHERE a.estimation > 0 AND c.montant > 0"
    )
    n = len(df)
    if n == 0:
        return {"mu": prior_mu, "sigma": prior_sigma, "n": 0}
    ratios = df["montant"] / df["estimation"]
    w = n / (n + POIDS_PRIOR)
    std = float(ratios.std(ddof=0)) if n > 1 else prior_sigma
    mu = w * float(ratios.mean()) + (1 - w) * prior_mu
    sigma = max(w * std + (1 - w) * prior_sigma, 0.02)
    return {"mu": mu, "sigma": sigma, "n": n}


def win_probability(
    price: float, estimation: float, model: dict, n_unknown: int, known_bids: list[float]
) -> float:
    """Probabilité d'être le moins-disant avec ce prix.

    * offres connues : on gagne seulement si on passe sous la plus basse ;
    * concurrents encore inconnus : on suppose des offres ~ Normale(mu, sigma) autour de l'estimation.
    """
    if known_bids and price >= min(known_bids):
        return 0.0
    if estimation <= 0:
        return 0.0
    p_beat_one = 1.0 - norm_cdf((price / estimation - model["mu"]) / model["sigma"])
    return p_beat_one ** max(n_unknown, 0)


def evaluate_prices(
    prices, estimation: float, cout: float, model: dict, n_unknown: int, known_bids: list[float]
) -> pd.DataFrame:
    rows = []
    for p in prices:
        pw = win_probability(float(p), estimation, model, n_unknown, known_bids)
        rows.append(
            {
                "prix": float(p),
                "proba": pw,
                "esperance": pw * (float(p) - cout),
                "marge_pct": (float(p) - cout) / cout * 100 if cout > 0 else 0.0,
            }
        )
    return pd.DataFrame(rows)


def optimise_price(
    estimation: float,
    cout: float,
    model: dict,
    n_unknown: int,
    known_bids: list[float],
    marge_min_pct: float,
    plafond_pct: float,
) -> dict | None:
    """Prix qui maximise le gain espéré = probabilité de gain x marge. None si le marché n'est pas rentable."""
    if estimation <= 0 or cout <= 0:
        return None
    plancher = cout * (1 + marge_min_pct / 100)
    plafond = estimation * plafond_pct / 100
    if plancher > plafond:
        return None
    prices = list(np.linspace(plancher, plafond, 150))
    if known_bids:
        juste_sous = min(known_bids) * 0.995
        if plancher <= juste_sous <= plafond:
            prices.append(juste_sous)
    df = evaluate_prices(prices, estimation, cout, model, n_unknown, known_bids)
    best = df.loc[df["esperance"].idxmax()]
    return {
        "prix": float(best["prix"]),
        "proba": float(best["proba"]),
        "marge_pct": float(best["marge_pct"]),
        "esperance": float(best["esperance"]),
    }


def verdict(best: dict | None) -> str:
    if best is None:
        return "⛔ Non rentable"
    if best["proba"] >= 0.5:
        return "🟢 Favorable"
    if best["proba"] >= 0.25:
        return "🟠 Incertain"
    return "🔴 Difficile"


def known_bids_by_tender() -> dict[int, list[float]]:
    bids = query("SELECT appel_id, montant FROM concurrents_offres")
    out: dict[int, list[float]] = {}
    for _, r in bids.iterrows():
        out.setdefault(int(r["appel_id"]), []).append(float(r["montant"]))
    return out


def rank_opportunities(model: dict, marge_min: float, plafond: float) -> tuple[pd.DataFrame, list[str]]:
    """Classe les marchés ouverts par gain espéré décroissant."""
    tenders = query("SELECT * FROM appels_offres WHERE statut IN ('À étudier', 'Soumis')")
    bids = known_bids_by_tender()
    rows, incomplets = [], []
    for _, t in tenders.iterrows():
        estimation, cout = float(t["estimation"]), float(t["cout_estime"])
        if estimation <= 0 or cout <= 0:
            incomplets.append(str(t["reference"]))
            continue
        known = bids.get(int(t["id"]), [])
        n_unknown = max(int(t["nb_concurrents"]) - len(known), 0)
        best = optimise_price(estimation, cout, model, n_unknown, known, marge_min, plafond)
        offre = float(t["notre_offre"])
        proba_offre = (
            win_probability(offre, estimation, model, n_unknown, known)
            if t["statut"] == "Soumis" and offre > 0
            else np.nan
        )
        rows.append(
            {
                "Référence": t["reference"],
                "Intitulé": t["intitule"],
                "Statut": t["statut"],
                "Date limite": t["date_limite"],
                "Estimation": estimation,
                "Coût estimé": cout,
                "Prix conseillé": best["prix"] if best else np.nan,
                "Proba de gain": best["proba"] * 100 if best else 0.0,
                "Marge %": best["marge_pct"] if best else np.nan,
                "Gain espéré": best["esperance"] if best else 0.0,
                "Verdict": verdict(best),
                "Proba avec votre offre": proba_offre * 100 if not pd.isna(proba_offre) else np.nan,
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("Gain espéré", ascending=False).reset_index(drop=True)
        df.insert(0, "Rang", range(1, len(df) + 1))
    return df, incomplets


# --------------------------------------------------------------------------- #
# Marchés BTP : données d'exemple
# --------------------------------------------------------------------------- #
DEMO_AO_CLOS = [
    {
        "reference": "AO-2025-011",
        "intitule": "Construction d'une école de 6 salles de classe",
        "lieu": "Garoua",
        "date": "2025-09-15",
        "estimation": 85_000_000,
        "cout": 68_000_000,
        "offre": 79_500_000,
        "bids": [("Alpha BTP", 81_000_000), ("Sahel Travaux", 76_800_000), ("Littoral Construction", 84_200_000)],
    },
    {
        "reference": "AO-2025-017",
        "intitule": "Réhabilitation d'un lycée technique",
        "lieu": "Douala",
        "date": "2025-11-04",
        "estimation": 120_000_000,
        "cout": 95_000_000,
        "offre": 108_000_000,
        "bids": [("Alpha BTP", 112_000_000), ("Littoral Construction", 110_500_000), ("Équatoriale Bâtiment", 115_000_000)],
    },
    {
        "reference": "AO-2025-023",
        "intitule": "Voirie et assainissement d'un quartier",
        "lieu": "Yaoundé",
        "date": "2026-01-22",
        "estimation": 210_000_000,
        "cout": 172_000_000,
        "offre": 195_000_000,
        "bids": [
            ("Équatoriale Bâtiment", 189_000_000),
            ("Alpha BTP", 198_000_000),
            ("Sahel Travaux", 201_000_000),
            ("Littoral Construction", 192_000_000),
        ],
    },
    {
        "reference": "AO-2026-004",
        "intitule": "Magasin de stockage communal",
        "lieu": "Bafoussam",
        "date": "2026-04-10",
        "estimation": 64_000_000,
        "cout": 50_000_000,
        "offre": 58_500_000,
        "bids": [("Alpha BTP", 60_200_000), ("Sahel Travaux", 57_900_000)],
    },
    {
        "reference": "AO-2026-009",
        "intitule": "Centre de santé intégré",
        "lieu": "Bertoua",
        "date": "2026-06-18",
        "estimation": 98_000_000,
        "cout": 77_000_000,
        "offre": 88_900_000,
        "bids": [("Littoral Construction", 91_500_000), ("Équatoriale Bâtiment", 93_000_000), ("Alpha BTP", 89_400_000)],
    },
]
DEMO_AO_OUVERTS = [
    {
        "reference": "AO-2026-015",
        "intitule": "Construction de 24 logements sociaux",
        "lieu": "Douala",
        "date": "2026-11-20",
        "estimation": 340_000_000,
        "cout": 275_000_000,
        "nb": 4,
        "bids": [("Alpha BTP", 318_000_000)],
    },
    {
        "reference": "AO-2026-018",
        "intitule": "Adduction d'eau et château d'eau",
        "lieu": "Maroua",
        "date": "2026-12-05",
        "estimation": 150_000_000,
        "cout": 128_000_000,
        "nb": 3,
        "bids": [],
    },
    {
        "reference": "AO-2026-021",
        "intitule": "Extension d'un bâtiment administratif",
        "lieu": "Yaoundé",
        "date": "2026-11-30",
        "estimation": 72_000_000,
        "cout": 66_500_000,
        "nb": 5,
        "bids": [],
    },
]
DEMO_PRIX_BASE = [
    ("Ciment CPJ 42,5 (sac 50 kg)", "sac", 6300),
    ("Fer à béton HA12 (barre 12 m)", "u", 8200),
    ("Fer à béton HA8 (barre 12 m)", "u", 3900),
    ("Sable de rivière", "m³", 12000),
    ("Gravier 15/25", "m³", 25000),
    ("Parpaing 15 cm", "u", 520),
    ("Tube PVC Ø100 (6 m)", "u", 9500),
    ("Peinture vinylique (seau 20 L)", "u", 38000),
]
DEMO_FOURNISSEURS = [("Quincaillerie Centrale", 1.00), ("Dépôt Matériaux Express", 0.97), ("Grossiste Littoral", 1.04)]
DEMO_RELEVES = [("2026-08-12", 0.0), ("2026-09-14", 0.02), ("2026-10-10", 0.045)]


def seed_marches_demo() -> tuple[int, int]:
    """Insère des appels d'offres, offres concurrentes et prix d'exemple. Retourne (marchés, relevés de prix)."""
    conn = get_connection()
    n_ao = n_prix = 0
    with _WRITE_LOCK:
        try:
            for ao in DEMO_AO_CLOS + DEMO_AO_OUVERTS:
                if conn.execute("SELECT 1 FROM appels_offres WHERE reference = ?", (ao["reference"],)).fetchone():
                    continue
                cloture = "offre" in ao
                if cloture:
                    tous = list(ao["bids"]) + [("Innovation BTP", ao["offre"])]
                    gagnant = min(tous, key=lambda x: x[1])[0]
                    statut = "Gagné" if gagnant == "Innovation BTP" else "Perdu"
                    offre, nb = ao["offre"], len(ao["bids"])
                else:
                    gagnant, statut, offre, nb = None, "À étudier", 0, ao["nb"]
                cur = conn.execute(
                    "INSERT INTO appels_offres (reference, intitule, maitre_ouvrage, localisation, date_limite, "
                    "estimation, cout_estime, notre_offre, nb_concurrents, statut, attributaire, notes) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        ao["reference"], ao["intitule"], "Maître d'ouvrage (exemple)", ao["lieu"], ao["date"],
                        ao["estimation"], ao["cout"], offre, nb, statut, gagnant,
                        "Donnée d'exemple : à remplacer par vos vrais marchés.",
                    ),
                )
                for entreprise, montant in ao["bids"]:
                    conn.execute(
                        "INSERT INTO concurrents_offres (appel_id, entreprise, montant) VALUES (?,?,?)",
                        (cur.lastrowid, entreprise, montant),
                    )
                n_ao += 1
            if conn.execute("SELECT COUNT(*) FROM prix_marche").fetchone()[0] == 0:
                for idx, (mat, unite, base) in enumerate(DEMO_PRIX_BASE):
                    amplitude = (1.5, 1.0, 0.3)[idx % 3]
                    for fournisseur, coef in DEMO_FOURNISSEURS:
                        for jour, derive in DEMO_RELEVES:
                            prix = round(base * coef * (1 + derive * amplitude) / 50) * 50
                            conn.execute(
                                "INSERT INTO prix_marche (materiau, unite, fournisseur, prix, date) VALUES (?,?,?,?,?)",
                                (mat, unite, fournisseur, max(prix, 50), jour),
                            )
                            n_prix += 1
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            log.exception("Création des données d'exemple annulée")
            st.error(f"Opération annulée : {exc}")
            return 0, 0
    return n_ao, n_prix


# --------------------------------------------------------------------------- #
# Marchés BTP : interface
# --------------------------------------------------------------------------- #
def tender_fields(prefix: str, row: pd.Series | None) -> dict:
    """Champs d'un appel d'offres (à utiliser dans un st.form)."""

    def val(key: str, default):
        return row[key] if row is not None and pd.notna(row[key]) else default

    reference = st.text_input("Référence *", value=str(val("reference", "")), key=f"{prefix}_ref")
    intitule = st.text_input("Intitulé du marché *", value=str(val("intitule", "")), key=f"{prefix}_int")
    col1, col2 = st.columns(2)
    maitre = col1.text_input("Maître d'ouvrage", value=str(val("maitre_ouvrage", "")), key=f"{prefix}_mo")
    lieu = col2.text_input("Localisation", value=str(val("localisation", "")), key=f"{prefix}_lieu")
    col3, col4 = st.columns(2)
    limite = col3.date_input(
        "Date limite de dépôt",
        value=to_date(row["date_limite"]) if row is not None else date.today(),
        key=f"{prefix}_lim",
    )
    estimation = col4.number_input(
        f"Estimation du maître d'ouvrage ({DEVISE})",
        min_value=0.0,
        step=1_000_000.0,
        format="%.0f",
        value=float(val("estimation", 0.0)),
        key=f"{prefix}_est",
    )
    col5, col6, col7 = st.columns(3)
    cout = col5.number_input(
        f"Votre coût estimé ({DEVISE})",
        min_value=0.0,
        step=1_000_000.0,
        format="%.0f",
        value=float(val("cout_estime", 0.0)),
        key=f"{prefix}_cout",
    )
    offre = col6.number_input(
        f"Votre offre déposée ({DEVISE}, 0 si aucune)",
        min_value=0.0,
        step=1_000_000.0,
        format="%.0f",
        value=float(val("notre_offre", 0.0)),
        key=f"{prefix}_offre",
    )
    nb = col7.number_input(
        "Concurrents attendus", min_value=0, max_value=30, value=int(val("nb_concurrents", 4)), step=1, key=f"{prefix}_nb"
    )
    col8, col9 = st.columns(2)
    statut_actuel = str(val("statut", "À étudier"))
    statut = col8.selectbox(
        "Statut",
        STATUTS_AO,
        index=STATUTS_AO.index(statut_actuel) if statut_actuel in STATUTS_AO else 0,
        key=f"{prefix}_statut",
    )
    attributaire = col9.text_input("Entreprise attributaire (si connue)", value=str(val("attributaire", "")), key=f"{prefix}_attr")
    notes = st.text_area("Notes", value=str(val("notes", "")), key=f"{prefix}_notes")
    return {
        "reference": reference.strip(),
        "intitule": intitule.strip(),
        "maitre_ouvrage": maitre.strip(),
        "localisation": lieu.strip(),
        "date_limite": limite.isoformat(),
        "estimation": estimation,
        "cout_estime": cout,
        "notre_offre": offre,
        "nb_concurrents": int(nb),
        "statut": statut,
        "attributaire": attributaire.strip(),
        "notes": notes.strip(),
    }


def validate_tender(data: dict) -> bool:
    if not data["reference"] or not data["intitule"]:
        st.error("La référence et l'intitulé sont obligatoires.")
        return False
    return True


def tab_marches_classement(model: dict, marge_min: float, plafond: float) -> None:
    st.write(
        "Pour chaque marché ouvert, l'application cherche le **prix qui maximise le gain espéré** "
        "(probabilité de gagner × marge), puis classe les marchés du plus au moins intéressant."
    )
    st.caption(
        "Hypothèse : le marché est attribué au moins-disant. Il s'agit d'une estimation statistique, "
        "pas d'une garantie : elle s'améliore à mesure que vous enregistrez des offres concurrentes."
    )
    all_ao = query("SELECT statut, notre_offre FROM appels_offres")
    if not all_ao.empty:
        clos = all_ao[all_ao["statut"].isin(["Gagné", "Perdu"])]
        c1, c2, c3 = st.columns(3)
        c1.metric("Marchés suivis", len(all_ao))
        c2.metric(
            "Taux de réussite historique",
            f"{(clos['statut'] == 'Gagné').mean():.0%}" if not clos.empty else "—",
        )
        c3.metric("Montant gagné", fcfa(clos.loc[clos["statut"] == "Gagné", "notre_offre"].sum() if not clos.empty else 0))

    ranking, incomplets = rank_opportunities(model, marge_min, plafond)
    if ranking.empty:
        st.info(
            "Aucun marché ouvert à analyser. Ajoutez un appel d'offres (statut « À étudier ») avec l'estimation "
            "du maître d'ouvrage et votre coût estimé, ou chargez les données d'exemple."
        )
    else:
        top = ranking.iloc[0]
        if top["Gain espéré"] > 0:
            st.success(
                f"**Meilleure option : {top['Référence']} — {top['Intitulé']}.** Prix conseillé {fcfa(top['Prix conseillé'])}, "
                f"probabilité de gain {top['Proba de gain']:.0f} %, marge {top['Marge %']:.1f} %, "
                f"gain espéré {fcfa(top['Gain espéré'])}."
            )
        else:
            st.warning("Aucun marché ouvert ne présente un gain espéré positif avec ces hypothèses.")
        st.dataframe(
            ranking,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Estimation": st.column_config.NumberColumn(format="%d"),
                "Coût estimé": st.column_config.NumberColumn(format="%d"),
                "Prix conseillé": st.column_config.NumberColumn(format="%d"),
                "Gain espéré": st.column_config.NumberColumn(format="%d"),
                "Marge %": st.column_config.NumberColumn(format="%.1f"),
                "Proba de gain": st.column_config.ProgressColumn("Proba de gain", min_value=0, max_value=100, format="%.0f%%"),
                "Proba avec votre offre": st.column_config.NumberColumn(format="%.0f%%"),
            },
        )
        st.bar_chart(ranking.set_index("Référence")["Gain espéré"])
    if incomplets:
        st.warning(
            "Marchés non analysés (estimation ou coût estimé manquant) : " + ", ".join(incomplets) + "."
        )


def tab_marches_ao() -> None:
    df = query(
        "SELECT id, reference, intitule, maitre_ouvrage, localisation, date_limite, estimation, cout_estime, "
        "notre_offre, nb_concurrents, statut, attributaire FROM appels_offres ORDER BY date_limite DESC"
    )
    if df.empty:
        st.info("Aucun appel d'offres enregistré.")
    else:
        statuts = st.multiselect("Filtrer par statut", STATUTS_AO, key="ao_filtre")
        shown = df[df["statut"].isin(statuts)] if statuts else df
        st.dataframe(
            shown,
            hide_index=True,
            use_container_width=True,
            column_config={
                "estimation": st.column_config.NumberColumn("Estimation", format="%d"),
                "cout_estime": st.column_config.NumberColumn("Coût estimé", format="%d"),
                "notre_offre": st.column_config.NumberColumn("Notre offre", format="%d"),
            },
        )
        st.caption(f"{len(shown)} appel(s) d'offres")

    if not can_edit():
        read_only_notice()
        return

    with st.expander("➕ Nouvel appel d'offres"):
        with st.form("ao_add", clear_on_submit=True):
            data = tender_fields("ao_add", None)
            if st.form_submit_button("Enregistrer", type="primary") and validate_tender(data):
                if db_write(
                    "INSERT INTO appels_offres (reference, intitule, maitre_ouvrage, localisation, date_limite, "
                    "estimation, cout_estime, notre_offre, nb_concurrents, statut, attributaire, notes) VALUES "
                    "(:reference,:intitule,:maitre_ouvrage,:localisation,:date_limite,:estimation,:cout_estime,"
                    ":notre_offre,:nb_concurrents,:statut,:attributaire,:notes)",
                    data,
                ) is not None:
                    flash("Appel d'offres enregistré.")
                    st.rerun()

    if df.empty:
        return
    labels = {int(r["id"]): f"{r['reference']} — {r['intitule']}" for _, r in df.iterrows()}
    tid = st.selectbox("Appel d'offres à modifier / compléter", list(labels), format_func=labels.get, key="ao_sel")
    row = query("SELECT * FROM appels_offres WHERE id = ?", (tid,)).iloc[0]

    with st.expander("✏️ Modifier cet appel d'offres"):
        with st.form(f"ao_edit_{tid}"):
            data = tender_fields(f"ao_edit_{tid}", row)
            if st.form_submit_button("Mettre à jour", type="primary") and validate_tender(data):
                data["id"] = tid
                if db_write(
                    "UPDATE appels_offres SET reference=:reference, intitule=:intitule, maitre_ouvrage=:maitre_ouvrage, "
                    "localisation=:localisation, date_limite=:date_limite, estimation=:estimation, "
                    "cout_estime=:cout_estime, notre_offre=:notre_offre, nb_concurrents=:nb_concurrents, "
                    "statut=:statut, attributaire=:attributaire, notes=:notes WHERE id=:id",
                    data,
                ) is not None:
                    flash("Appel d'offres mis à jour.")
                    st.rerun()

    st.write("**Offres des concurrents sur ce marché**")
    bids = query(
        "SELECT id, entreprise, montant FROM concurrents_offres WHERE appel_id = ? ORDER BY montant", (tid,)
    )
    if bids.empty:
        st.caption("Aucune offre concurrente enregistrée.")
    else:
        shown_bids = bids.copy()
        if float(row["estimation"]) > 0:
            shown_bids["% de l'estimation"] = (shown_bids["montant"] / float(row["estimation"]) * 100).round(1)
        st.dataframe(shown_bids, hide_index=True, use_container_width=True)
    with st.form(f"bid_add_{tid}", clear_on_submit=True):
        col1, col2 = st.columns(2)
        entreprise = col1.text_input("Entreprise concurrente *", key=f"bid_ent_{tid}")
        montant = col2.number_input(
            f"Montant de l'offre ({DEVISE}) *", min_value=0.0, step=1_000_000.0, format="%.0f", key=f"bid_mt_{tid}"
        )
        if st.form_submit_button("Ajouter l'offre concurrente"):
            if not entreprise.strip() or montant <= 0:
                st.error("Indiquez l'entreprise et un montant supérieur à zéro.")
            elif db_write(
                "INSERT INTO concurrents_offres (appel_id, entreprise, montant) VALUES (?,?,?)",
                (tid, entreprise.strip(), montant),
            ) is not None:
                flash("Offre concurrente ajoutée.")
                st.rerun()
    if not bids.empty:
        bid_labels = {int(r["id"]): f"{r['entreprise']} — {fcfa(r['montant'])}" for _, r in bids.iterrows()}
        bid_id = st.selectbox("Offre à supprimer", list(bid_labels), format_func=bid_labels.get, key=f"bid_del_{tid}")
        if st.button("Supprimer l'offre", key=f"bid_del_btn_{tid}"):
            if db_write("DELETE FROM concurrents_offres WHERE id = ?", (bid_id,)) is not None:
                flash("Offre supprimée.")
                st.rerun()

    if is_admin():
        st.divider()
        if confirm_delete(f"ao_{tid}") and st.button("Supprimer cet appel d'offres", key=f"del_ao_{tid}"):
            if db_write("DELETE FROM appels_offres WHERE id = ?", (tid,)) is not None:
                flash("Appel d'offres supprimé.")
                st.rerun()


def analyse_prix(prix: pd.DataFrame) -> pd.DataFrame:
    """Synthèse par matériau : meilleur prix, moyenne, écart entre fournisseurs, tendance mensuelle."""
    prix = prix.sort_values("date")
    dernier = prix.groupby(["materiau", "fournisseur"]).tail(1)
    rows = []
    for mat, g in dernier.groupby("materiau"):
        best = g.loc[g["prix"].idxmin()]
        rows.append(
            {
                "Matériau": mat,
                "Unité": g["unite"].iloc[0],
                "Meilleur prix": float(best["prix"]),
                "Fournisseur": best["fournisseur"],
                "Prix moyen": float(g["prix"].mean()),
                "Prix max": float(g["prix"].max()),
                "Écart max/min %": (float(g["prix"].max()) / float(g["prix"].min()) - 1) * 100,
                "Nb fournisseurs": len(g),
            }
        )
    out = pd.DataFrame(rows)
    mensuel = prix.assign(mois=prix["date"].str[:7]).groupby(["materiau", "mois"])["prix"].mean().reset_index()
    tendance: dict[str, float] = {}
    for mat, g in mensuel.groupby("materiau"):
        g = g.sort_values("mois")
        if len(g) >= 2:
            tendance[mat] = (float(g["prix"].iloc[-1]) / float(g["prix"].iloc[-2]) - 1) * 100
    out["Tendance mensuelle %"] = out["Matériau"].map(tendance)

    def signal(t: float) -> str:
        if pd.isna(t):
            return "—"
        if t > 3:
            return "📈 Hausse : acheter / sécuriser"
        if t < -3:
            return "📉 Baisse : patienter"
        return "➡️ Stable"

    out["Signal"] = out["Tendance mensuelle %"].map(signal)
    stock = query("SELECT nom, prix_unitaire FROM materiaux")
    if not stock.empty:
        stock_map = dict(zip(stock["nom"], stock["prix_unitaire"]))
        out["Prix en stock"] = out["Matériau"].map(stock_map)
        if out["Prix en stock"].notna().any():
            out["Écart stock/marché %"] = (out["Prix en stock"] / out["Meilleur prix"] - 1) * 100
        else:
            out = out.drop(columns="Prix en stock")
    return out.round(1)


def tab_marches_prix() -> None:
    prix = query("SELECT * FROM prix_marche ORDER BY date")
    if prix.empty:
        st.info("Aucun relevé de prix. Ajoutez des relevés ci-dessous ou chargez les données d'exemple.")
    else:
        synth = analyse_prix(prix)
        st.subheader("Synthèse par matériau")
        st.caption("Basée sur le dernier relevé de chaque fournisseur.")
        st.dataframe(synth, hide_index=True, use_container_width=True)

        mats = list(synth["Matériau"])
        choix = st.selectbox("Évolution des prix du matériau", mats, key="prix_mat")
        g = prix[prix["materiau"] == choix]
        st.line_chart(g.pivot_table(index="date", columns="fournisseur", values="prix", aggfunc="mean"))

        st.subheader("Estimateur du coût matériaux")
        st.caption("Ajoutez des lignes (matériau + quantité) pour comparer l'achat au meilleur prix et au prix moyen.")
        base = pd.DataFrame({"Matériau": mats[:1], "Quantité": [1.0]})
        edited = st.data_editor(
            base,
            num_rows="dynamic",
            hide_index=True,
            use_container_width=True,
            key="est_mat",
            column_config={
                "Matériau": st.column_config.SelectboxColumn("Matériau", options=mats, required=True),
                "Quantité": st.column_config.NumberColumn("Quantité", min_value=0.0),
            },
        )
        lignes = edited.dropna(subset=["Matériau"]).merge(
            synth[["Matériau", "Unité", "Meilleur prix", "Prix moyen"]], on="Matériau", how="left"
        )
        if not lignes.empty:
            lignes["Quantité"] = lignes["Quantité"].fillna(0)
            lignes["Total meilleur prix"] = lignes["Quantité"] * lignes["Meilleur prix"]
            lignes["Total prix moyen"] = lignes["Quantité"] * lignes["Prix moyen"]
            st.dataframe(lignes, hide_index=True, use_container_width=True)
            total_best, total_avg = float(lignes["Total meilleur prix"].sum()), float(lignes["Total prix moyen"].sum())
            c1, c2, c3 = st.columns(3)
            c1.metric("Coût au meilleur prix", fcfa(total_best))
            c2.metric("Coût au prix moyen", fcfa(total_avg))
            c3.metric("Économie possible", fcfa(total_avg - total_best))

    if not can_edit():
        read_only_notice()
        return
    with st.expander("➕ Ajouter un relevé de prix"):
        with st.form("prix_add", clear_on_submit=True):
            col1, col2 = st.columns(2)
            materiau = col1.text_input("Matériau *", key="prix_add_mat")
            unite = col2.selectbox("Unité", UNITES, key="prix_add_unite")
            col3, col4, col5 = st.columns(3)
            fournisseur = col3.text_input("Fournisseur *", key="prix_add_four")
            valeur = col4.number_input(f"Prix unitaire ({DEVISE}) *", min_value=0.0, step=50.0, format="%.0f", key="prix_add_val")
            jour = col5.date_input("Date du relevé", value=date.today(), key="prix_add_date")
            if st.form_submit_button("Enregistrer le relevé", type="primary"):
                if not materiau.strip() or not fournisseur.strip() or valeur <= 0:
                    st.error("Matériau, fournisseur et un prix supérieur à zéro sont obligatoires.")
                elif db_write(
                    "INSERT INTO prix_marche (materiau, unite, fournisseur, prix, date) VALUES (?,?,?,?,?)",
                    (materiau.strip(), unite, fournisseur.strip(), valeur, jour.isoformat()),
                ) is not None:
                    flash("Relevé enregistré.")
                    st.rerun()
    if is_admin() and not prix.empty:
        with st.expander("🗑️ Supprimer un relevé"):
            recents = prix.sort_values("date", ascending=False).head(50)
            labels = {
                int(r["id"]): f"{r['date']} · {r['materiau']} · {r['fournisseur']} · {fcfa(r['prix'])}"
                for _, r in recents.iterrows()
            }
            pid = st.selectbox("Relevé (50 plus récents)", list(labels), format_func=labels.get, key="prix_del_sel")
            if st.button("Supprimer le relevé", key="prix_del_btn"):
                if db_write("DELETE FROM prix_marche WHERE id = ?", (pid,)) is not None:
                    flash("Relevé supprimé.")
                    st.rerun()


def tab_marches_concurrence(model: dict) -> None:
    df = query(
        "SELECT c.entreprise, c.montant, a.estimation, a.attributaire FROM concurrents_offres c "
        "JOIN appels_offres a ON a.id = c.appel_id WHERE a.estimation > 0"
    )
    if df.empty:
        st.info("Aucune offre concurrente enregistrée. Ajoutez-en dans l'onglet « Appels d'offres ».")
        return
    df["ratio"] = df["montant"] / df["estimation"] * 100
    df["gagne"] = (
        df["attributaire"].fillna("").str.strip().str.lower() == df["entreprise"].str.strip().str.lower()
    )
    stats = (
        df.groupby("entreprise")
        .agg(offres=("montant", "count"), ratio_moyen=("ratio", "mean"), ratio_min=("ratio", "min"), victoires=("gagne", "sum"))
        .reset_index()
    )
    stats["taux_victoire"] = stats["victoires"] / stats["offres"] * 100
    global_mean = float(df["ratio"].mean())

    def profil(r: float) -> str:
        if r < global_mean - 2:
            return "💣 Agressif (prix bas)"
        if r > global_mean + 2:
            return "🛡️ Prudent (prix élevés)"
        return "⚖️ Dans la moyenne"

    stats["Profil"] = stats["ratio_moyen"].map(profil)
    stats = stats.rename(
        columns={
            "entreprise": "Entreprise",
            "offres": "Offres",
            "ratio_moyen": "Offre moyenne (% estimation)",
            "ratio_min": "Offre la plus basse (%)",
            "victoires": "Victoires",
            "taux_victoire": "Taux de victoire %",
        }
    ).sort_values("Offre moyenne (% estimation)")
    st.dataframe(stats.round(1), hide_index=True, use_container_width=True)
    st.bar_chart(stats.set_index("Entreprise")["Offre moyenne (% estimation)"])
    st.caption(
        f"Offre concurrente moyenne retenue par le modèle : {model['mu']:.1%} de l'estimation "
        f"(écart-type {model['sigma']:.1%}) sur {model['n']} offre(s)."
    )


def tab_marches_simulateur(model: dict, marge_min: float, plafond: float) -> None:
    tenders = query("SELECT * FROM appels_offres ORDER BY date_limite DESC")
    if tenders.empty:
        st.info("Ajoutez d'abord un appel d'offres.")
        return
    labels = {int(r["id"]): f"{r['reference']} — {r['intitule']}" for _, r in tenders.iterrows()}
    tid = st.selectbox("Marché à simuler", list(labels), format_func=labels.get, key="sim_tid")
    t = tenders[tenders["id"] == tid].iloc[0]
    known = [float(x) for x in query("SELECT montant FROM concurrents_offres WHERE appel_id = ?", (tid,))["montant"]]
    estimation, cout = float(t["estimation"]), float(t["cout_estime"])
    if estimation <= 0 or cout <= 0:
        st.warning("Renseignez l'estimation du maître d'ouvrage et votre coût estimé dans l'onglet « Appels d'offres ».")
        return

    c1, c2, c3 = st.columns(3)
    n_total = int(c1.number_input("Concurrents attendus", 0, 30, int(t["nb_concurrents"]), key=f"sim_n_{tid}"))
    var_mat = c2.slider("Variation du prix des matériaux (%)", -20, 30, 0, key=f"sim_var_{tid}")
    part_mat = c3.slider("Part des matériaux dans votre coût (%)", 0, 90, 55, key=f"sim_part_{tid}")
    cout_adj = cout * (1 + part_mat / 100 * var_mat / 100)
    n_unknown = max(n_total - len(known), 0)
    st.caption(
        f"Coût ajusté : {fcfa(cout_adj)} · {len(known)} offre(s) concurrente(s) connue(s) · "
        f"{n_unknown} concurrent(s) encore inconnu(s)."
    )

    best = optimise_price(estimation, cout_adj, model, n_unknown, known, marge_min, plafond)
    if best is None:
        st.error(
            "Marché non rentable : votre coût (avec la marge minimale) dépasse le plafond autorisé. "
            "Réduisez le coût, ou relevez le plafond dans les hypothèses."
        )
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Prix conseillé", fcfa(best["prix"]))
        m2.metric("Probabilité de gain", f"{best['proba']:.0%}")
        m3.metric("Marge", f"{best['marge_pct']:.1f} %")
        m4.metric("Gain espéré", fcfa(best["esperance"]))
        st.write(f"**Verdict : {verdict(best)}**")
        if known and best["proba"] == 0:
            st.warning("Un concurrent est déjà sous votre prix plancher : ce marché est très difficile à gagner.")

    prix_test = st.number_input(
        f"Tester un prix ({DEVISE})",
        min_value=0.0,
        step=500_000.0,
        format="%.0f",
        value=float(round(best["prix"])) if best else float(round(estimation)),
        key=f"sim_test_{tid}",
    )
    if prix_test > 0:
        pw = win_probability(prix_test, estimation, model, n_unknown, known)
        st.write(
            f"À {fcfa(prix_test)} : probabilité de gain **{pw:.0%}**, marge **{(prix_test - cout_adj) / cout_adj:.1%}**, "
            f"gain espéré **{fcfa(pw * (prix_test - cout_adj))}**."
        )

    bas = estimation * 0.6
    haut = max(estimation * plafond / 100, bas * 1.01)
    courbe = evaluate_prices(np.linspace(bas, haut, 80), estimation, cout_adj, model, n_unknown, known)
    courbe["Prix (M FCFA)"] = (courbe["prix"] / 1_000_000).round(1)
    courbe = courbe.set_index("Prix (M FCFA)")
    st.write("**Probabilité de gain selon le prix proposé (%)**")
    st.line_chart((courbe["proba"] * 100).rename("Probabilité de gain (%)"))
    st.write("**Gain espéré selon le prix proposé (FCFA)**")
    st.line_chart(courbe["esperance"].rename("Gain espéré"))


def page_marches() -> None:
    st.header("📈 Analyse des marchés BTP")
    st.caption("Prix des marchés, prix des matériaux et concurrence : probabilité de gain et meilleures options.")

    with st.expander("⚙️ Hypothèses du modèle"):
        c1, c2, c3, c4 = st.columns(4)
        marge_min = c1.number_input("Marge minimale visée (%)", 0.0, 50.0, 8.0, 0.5, key="mk_marge")
        plafond = c2.number_input("Plafond (% de l'estimation)", 50.0, 130.0, 100.0, 1.0, key="mk_plafond")
        prior_mu = c3.number_input("Offre concurrente typique (% estimation)", 50.0, 130.0, 92.0, 1.0, key="mk_mu") / 100
        prior_sigma = c4.number_input("Dispersion des offres (points %)", 1.0, 30.0, 7.0, 0.5, key="mk_sigma") / 100
        st.caption(
            "Ces valeurs servent de point de départ ; elles sont corrigées automatiquement par les offres "
            "concurrentes que vous enregistrez."
        )
    model = competitor_model(prior_mu, prior_sigma)
    st.caption(
        f"Modèle de concurrence : offre concurrente moyenne ≈ {model['mu']:.1%} de l'estimation "
        f"(± {model['sigma']:.1%}), d'après {model['n']} offre(s) enregistrée(s)."
    )

    tab_rank, tab_ao, tab_prix, tab_conc, tab_sim, tab_demo = st.tabs(
        ["🎯 Meilleures options", "📋 Appels d'offres", "🧱 Prix des matériaux", "🏢 Concurrence", "🎲 Simulateur", "Données d'exemple"]
    )
    with tab_rank:
        tab_marches_classement(model, marge_min, plafond)
    with tab_ao:
        tab_marches_ao()
    with tab_prix:
        tab_marches_prix()
    with tab_conc:
        tab_marches_concurrence(model)
    with tab_sim:
        tab_marches_simulateur(model, marge_min, plafond)
    with tab_demo:
        st.write(
            "Charge 5 marchés clôturés (avec les offres des concurrents), 3 marchés ouverts à étudier et "
            "des relevés de prix de 8 matériaux chez 3 fournisseurs."
        )
        st.caption("Valeurs fictives et indicatives : remplacez-les par vos relevés réels.")
        if not can_edit():
            read_only_notice()
        elif st.button("Charger les données d'exemple", type="primary", key="seed_mk"):
            n_ao, n_prix = seed_marches_demo()
            if n_ao or n_prix:
                flash(f"{n_ao} marché(s) et {n_prix} relevé(s) de prix ajoutés.")
                st.rerun()
            else:
                st.info("Les données d'exemple existent déjà.")


# --------------------------------------------------------------------------- #
# Devis
# --------------------------------------------------------------------------- #
def devis_totals(lines: pd.DataFrame, tva: float) -> tuple[float, float, float]:
    ht = float((lines["quantite"] * lines["prix_unitaire"]).sum()) if not lines.empty else 0.0
    montant_tva = ht * tva / 100
    return ht, montant_tva, ht + montant_tva


def next_devis_number(devis_id: int) -> str:
    return f"DV-{date.today().year}-{devis_id:04d}"


def page_devis() -> None:
    st.header("🧾 Devis")
    clients = choice_map("SELECT id, nom FROM clients ORDER BY nom")
    chantiers = choice_map("SELECT id, nom FROM chantiers ORDER BY nom")
    tab_list, tab_new, tab_detail = st.tabs(["Liste", "Nouveau devis", "Détail / lignes"])

    with tab_list:
        df = query(
            """
            SELECT d.id, d.numero, cl.nom AS client, ch.nom AS chantier, d.date, d.statut,
                   COALESCE(SUM(l.quantite * l.prix_unitaire), 0) AS total_ht, d.tva
            FROM devis d
            LEFT JOIN clients cl ON cl.id = d.client_id
            LEFT JOIN chantiers ch ON ch.id = d.chantier_id
            LEFT JOIN devis_lignes l ON l.devis_id = d.id
            GROUP BY d.id ORDER BY d.date DESC, d.id DESC
            """
        )
        if not df.empty:
            df["total_ttc"] = df["total_ht"] * (1 + df["tva"] / 100)
        st.dataframe(df, hide_index=True, use_container_width=True)
        if not df.empty:
            accepted = df[df["statut"] == "Accepté"]["total_ttc"].sum()
            st.metric("Total des devis acceptés (TTC)", fcfa(accepted))

    with tab_new:
        if not can_edit():
            read_only_notice()
        elif not clients:
            st.info("Créez d'abord un client.")
        else:
            with st.form("devis_new", clear_on_submit=True):
                client_id = st.selectbox("Client *", list(clients), format_func=clients.get)
                chantier_opts = [0] + list(chantiers)
                chantier_id = st.selectbox(
                    "Chantier", chantier_opts, format_func=lambda i: "— Aucun —" if i == 0 else chantiers[i]
                )
                d = st.date_input("Date", value=date.today())
                tva = st.number_input("TVA (%)", min_value=0.0, max_value=100.0, value=TVA_DEFAUT, step=0.25)
                if st.form_submit_button("Créer le devis", type="primary"):
                    temp = f"TMP-{secrets.token_hex(4)}"
                    new_id = db_write(
                        "INSERT INTO devis (numero, client_id, chantier_id, date, statut, tva) "
                        "VALUES (?,?,?,?, 'Brouillon', ?)",
                        (temp, client_id, chantier_id or None, d.isoformat(), tva),
                    )
                    if new_id is not None:
                        db_write("UPDATE devis SET numero = ? WHERE id = ?", (next_devis_number(new_id), new_id))
                        flash("Devis créé : ajoutez les lignes dans l'onglet « Détail / lignes ».")
                        st.rerun()

    with tab_detail:
        devis = choice_map("SELECT id, numero || ' — ' || date FROM devis ORDER BY id DESC")
        if not devis:
            st.info("Aucun devis.")
        else:
            did = st.selectbox("Devis", list(devis), format_func=devis.get, key="devis_sel")
            show_devis_detail(did)


def show_devis_detail(did: int) -> None:
    head = query(
        "SELECT d.*, cl.nom AS client FROM devis d LEFT JOIN clients cl ON cl.id = d.client_id WHERE d.id = ?",
        (did,),
    ).iloc[0]
    lines = query(
        "SELECT id, designation, quantite, unite, prix_unitaire FROM devis_lignes WHERE devis_id = ? ORDER BY id",
        (did,),
    )
    st.subheader(f"{head['numero']} · {head['client'] or 'Client supprimé'}")
    if not lines.empty:
        shown = lines.assign(total=lines["quantite"] * lines["prix_unitaire"])
        st.dataframe(shown, hide_index=True, use_container_width=True)
    else:
        st.caption("Ce devis ne contient encore aucune ligne.")
    ht, tva_amount, ttc = devis_totals(lines, float(head["tva"]))
    c1, c2, c3 = st.columns(3)
    c1.metric("Total HT", fcfa(ht))
    c2.metric(f"TVA ({head['tva']:.2f} %)", fcfa(tva_amount))
    c3.metric("Total TTC", fcfa(ttc))

    if can_edit():
        with st.form(f"line_add_{did}", clear_on_submit=True):
            st.write("**Ajouter une ligne**")
            designation = st.text_input("Désignation *")
            col1, col2, col3 = st.columns(3)
            quantite = col1.number_input("Quantité", min_value=0.0, value=1.0, step=1.0)
            unite = col2.selectbox("Unité", UNITES)
            prix = col3.number_input(f"Prix unitaire ({DEVISE})", min_value=0.0, step=1000.0, format="%.0f")
            if st.form_submit_button("Ajouter la ligne", type="primary"):
                if not designation.strip():
                    st.error("La désignation est obligatoire.")
                elif quantite <= 0:
                    st.error("La quantité doit être supérieure à zéro.")
                elif db_write(
                    "INSERT INTO devis_lignes (devis_id, designation, quantite, unite, prix_unitaire) "
                    "VALUES (?,?,?,?,?)",
                    (did, designation.strip(), quantite, unite, prix),
                ) is not None:
                    flash("Ligne ajoutée.")
                    st.rerun()

        col_a, col_b = st.columns(2)
        with col_a:
            new_statut = st.selectbox(
                "Statut du devis",
                STATUTS_DEVIS,
                index=STATUTS_DEVIS.index(head["statut"]) if head["statut"] in STATUTS_DEVIS else 0,
                key=f"devis_statut_{did}",
            )
            if new_statut != head["statut"] and st.button("Appliquer le statut", key=f"devis_apply_{did}"):
                if db_write("UPDATE devis SET statut = ? WHERE id = ?", (new_statut, did)) is not None:
                    flash("Statut mis à jour.")
                    st.rerun()
        with col_b:
            if not lines.empty:
                labels = {int(r["id"]): f"{r['designation']} (x{r['quantite']:g})" for _, r in lines.iterrows()}
                lid = st.selectbox("Ligne à supprimer", list(labels), format_func=labels.get, key=f"line_del_{did}")
                if st.button("Supprimer la ligne", key=f"line_del_btn_{did}"):
                    if db_write("DELETE FROM devis_lignes WHERE id = ?", (lid,)) is not None:
                        flash("Ligne supprimée.")
                        st.rerun()

    if not lines.empty:
        export = lines.drop(columns=["id"]).assign(total=lines["quantite"] * lines["prix_unitaire"])
        summary = pd.DataFrame(
            {"designation": ["TOTAL HT", f"TVA {head['tva']:.2f} %", "TOTAL TTC"], "total": [ht, tva_amount, ttc]}
        )
        st.download_button(
            "⬇️ Télécharger le devis (Excel)",
            data=excel_bytes({"Devis": pd.concat([export, summary], ignore_index=True)}),
            file_name=f"{head['numero']}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    if is_admin():
        st.divider()
        if confirm_delete(f"devis_{did}") and st.button("Supprimer ce devis", key=f"del_devis_{did}"):
            if db_write("DELETE FROM devis WHERE id = ?", (did,)) is not None:
                flash("Devis supprimé.")
                st.rerun()


# --------------------------------------------------------------------------- #
# Stock
# --------------------------------------------------------------------------- #
def page_stock() -> None:
    st.header("📦 Stock de matériaux")
    tab_list, tab_add, tab_move, tab_hist = st.tabs(["État du stock", "Nouveau matériau", "Entrée / sortie", "Historique"])

    with tab_list:
        df = query("SELECT id, nom, unite, quantite, seuil_alerte, prix_unitaire FROM materiaux ORDER BY nom")
        if df.empty:
            st.info("Aucun matériau enregistré.")
        else:
            df["valeur"] = df["quantite"] * df["prix_unitaire"]
            df["alerte"] = df.apply(lambda r: "⚠️ Bas" if r["quantite"] <= r["seuil_alerte"] else "OK", axis=1)
            st.dataframe(df, hide_index=True, use_container_width=True)
            st.metric("Valeur totale du stock", fcfa(df["valeur"].sum()))
        if can_edit():
            st.divider()
            st.caption("Le stock d'exemple (14 matériaux, entrées et sorties vers les 4 chantiers) est créé au premier lancement.")
            if st.button("Charger / compléter le stock d'exemple", key="seed_stock"):
                n_mat, n_mvt = seed_stock_demo()
                if n_mat:
                    flash(f"{n_mat} matériau(x) et {n_mvt} mouvement(s) ajoutés.")
                    st.rerun()
                else:
                    st.info("Le stock d'exemple est déjà présent.")

    with tab_add:
        if not can_edit():
            read_only_notice()
        else:
            with st.form("mat_add", clear_on_submit=True):
                nom = st.text_input("Nom du matériau *")
                col1, col2, col3 = st.columns(3)
                unite = col1.selectbox("Unité", UNITES)
                seuil = col2.number_input("Seuil d'alerte", min_value=0.0, step=1.0)
                prix = col3.number_input(f"Prix unitaire ({DEVISE})", min_value=0.0, step=100.0, format="%.0f")
                quantite = st.number_input("Quantité initiale", min_value=0.0, step=1.0)
                if st.form_submit_button("Enregistrer", type="primary"):
                    if not nom.strip():
                        st.error("Le nom est obligatoire.")
                    elif db_write(
                        "INSERT INTO materiaux (nom, unite, quantite, seuil_alerte, prix_unitaire) VALUES (?,?,?,?,?)",
                        (nom.strip(), unite, quantite, seuil, prix),
                    ) is not None:
                        flash("Matériau ajouté.")
                        st.rerun()

    with tab_move:
        materiaux = choice_map("SELECT id, nom FROM materiaux ORDER BY nom")
        chantiers = choice_map("SELECT id, nom FROM chantiers ORDER BY nom")
        if not materiaux:
            st.info("Ajoutez d'abord un matériau.")
        elif not can_edit():
            read_only_notice()
        else:
            with st.form("mat_move", clear_on_submit=True):
                mid = st.selectbox("Matériau", list(materiaux), format_func=materiaux.get)
                mtype = st.radio("Type", ["Entrée", "Sortie"], horizontal=True)
                qty = st.number_input("Quantité", min_value=0.0, step=1.0)
                options = [0] + list(chantiers)
                cid = st.selectbox(
                    "Chantier (pour les sorties)", options, format_func=lambda i: "— Aucun —" if i == 0 else chantiers[i]
                )
                when = st.date_input("Date", value=date.today())
                note = st.text_input("Note")
                if st.form_submit_button("Valider le mouvement", type="primary"):
                    current = float(query("SELECT quantite FROM materiaux WHERE id = ?", (mid,)).iloc[0, 0])
                    if qty <= 0:
                        st.error("La quantité doit être supérieure à zéro.")
                    elif mtype == "Sortie" and qty > current:
                        st.error(f"Stock insuffisant : {current:g} disponible(s).")
                    else:
                        delta = qty if mtype == "Entrée" else -qty
                        ok = db_transaction(
                            [
                                (
                                    "INSERT INTO mouvements_stock (materiau_id, chantier_id, type, quantite, date, note) "
                                    "VALUES (?,?,?,?,?,?)",
                                    (mid, cid or None, mtype, qty, when.isoformat(), note.strip()),
                                ),
                                ("UPDATE materiaux SET quantite = quantite + ? WHERE id = ?", (delta, mid)),
                            ]
                        )
                        if ok:
                            flash("Mouvement enregistré.")
                            st.rerun()

    with tab_hist:
        hist = query(
            """
            SELECT m.date, mat.nom AS materiau, m.type, m.quantite, ch.nom AS chantier, m.note
            FROM mouvements_stock m
            JOIN materiaux mat ON mat.id = m.materiau_id
            LEFT JOIN chantiers ch ON ch.id = m.chantier_id
            ORDER BY m.date DESC, m.id DESC
            """
        )
        st.dataframe(hist, hide_index=True, use_container_width=True)


# --------------------------------------------------------------------------- #
# Personnel et pointages
# --------------------------------------------------------------------------- #
def page_personnel() -> None:
    st.header("👷 Personnel")
    tab_list, tab_add, tab_pointage, tab_paie = st.tabs(["Employés", "Nouvel employé", "Pointage", "Coût main-d'œuvre"])

    with tab_list:
        df = query("SELECT id, nom, poste, telephone, taux_journalier, actif FROM employes ORDER BY nom")
        st.dataframe(df, hide_index=True, use_container_width=True)
        if can_edit() and not df.empty:
            labels = choice_map("SELECT id, nom FROM employes ORDER BY nom")
            eid = st.selectbox("Activer / désactiver", list(labels), format_func=labels.get, key="emp_toggle")
            if st.button("Inverser l'état", key="emp_toggle_btn"):
                if db_write("UPDATE employes SET actif = 1 - actif WHERE id = ?", (eid,)) is not None:
                    flash("État modifié.")
                    st.rerun()

    with tab_add:
        if not can_edit():
            read_only_notice()
        else:
            with st.form("emp_add", clear_on_submit=True):
                nom = st.text_input("Nom complet *")
                poste = st.text_input("Poste (maçon, ferrailleur, chef d'équipe…)")
                tel = st.text_input("Téléphone")
                taux = st.number_input(f"Taux journalier ({DEVISE})", min_value=0.0, step=500.0, format="%.0f")
                if st.form_submit_button("Enregistrer", type="primary"):
                    if not nom.strip():
                        st.error("Le nom est obligatoire.")
                    elif not valid_phone(tel):
                        st.error("Numéro de téléphone invalide.")
                    elif db_write(
                        "INSERT INTO employes (nom, poste, telephone, taux_journalier) VALUES (?,?,?,?)",
                        (nom.strip(), poste.strip(), tel.strip(), taux),
                    ) is not None:
                        flash("Employé ajouté.")
                        st.rerun()

    with tab_pointage:
        employes = choice_map("SELECT id, nom FROM employes WHERE actif = 1 ORDER BY nom")
        chantiers = choice_map("SELECT id, nom FROM chantiers WHERE statut IN ('Planifié','En cours') ORDER BY nom")
        if not employes or not chantiers:
            st.info("Il faut au moins un employé actif et un chantier ouvert pour pointer.")
        elif not can_edit():
            read_only_notice()
        else:
            with st.form("pointage_add", clear_on_submit=True):
                eid = st.selectbox("Employé", list(employes), format_func=employes.get)
                cid = st.selectbox("Chantier", list(chantiers), format_func=chantiers.get)
                when = st.date_input("Date", value=date.today())
                heures = st.number_input("Heures travaillées", min_value=0.5, max_value=24.0, value=8.0, step=0.5)
                if st.form_submit_button("Enregistrer le pointage", type="primary"):
                    deja = float(
                        query(
                            "SELECT COALESCE(SUM(heures), 0) FROM pointages WHERE employe_id = ? AND date = ?",
                            (eid, when.isoformat()),
                        ).iloc[0, 0]
                    )
                    if deja + heures > 24:
                        st.error(f"Total journalier supérieur à 24 h (déjà {deja:g} h ce jour-là).")
                    elif db_write(
                        "INSERT INTO pointages (employe_id, chantier_id, date, heures) VALUES (?,?,?,?)",
                        (eid, cid, when.isoformat(), heures),
                    ) is not None:
                        flash("Pointage enregistré.")
                        st.rerun()
        recent = query(
            "SELECT p.date, e.nom AS employe, ch.nom AS chantier, p.heures FROM pointages p "
            "JOIN employes e ON e.id = p.employe_id LEFT JOIN chantiers ch ON ch.id = p.chantier_id "
            "ORDER BY p.date DESC, p.id DESC LIMIT 50"
        )
        st.write("**50 derniers pointages**")
        st.dataframe(recent, hide_index=True, use_container_width=True)

    with tab_paie:
        df = query(
            """
            SELECT substr(p.date, 1, 7) AS mois, e.nom AS employe, COALESCE(ch.nom, '—') AS chantier,
                   SUM(p.heures) AS heures, e.taux_journalier AS taux
            FROM pointages p JOIN employes e ON e.id = p.employe_id
            LEFT JOIN chantiers ch ON ch.id = p.chantier_id
            GROUP BY mois, e.id, ch.id ORDER BY mois DESC, employe
            """
        )
        if df.empty:
            st.info("Aucun pointage.")
        else:
            df["cout"] = df["heures"] / HEURES_PAR_JOUR * df["taux"]
            st.dataframe(df, hide_index=True, use_container_width=True)
            st.metric("Coût total estimé", fcfa(df["cout"].sum()))
            st.bar_chart(df.groupby("mois")["cout"].sum())


# --------------------------------------------------------------------------- #
# Dépenses
# --------------------------------------------------------------------------- #
def page_depenses() -> None:
    st.header("💸 Dépenses")
    chantiers = choice_map("SELECT id, nom FROM chantiers ORDER BY nom")
    tab_list, tab_add = st.tabs(["Liste", "Nouvelle dépense"])

    with tab_list:
        col1, col2, col3 = st.columns(3)
        f_chantier = col1.selectbox(
            "Chantier", [0] + list(chantiers), format_func=lambda i: "Tous" if i == 0 else chantiers[i], key="dep_f_ch"
        )
        f_cat = col2.multiselect("Catégorie", CATEGORIES_DEPENSE, key="dep_f_cat")
        f_periode = col3.date_input("Période", value=(), key="dep_f_per")
        df = query(
            "SELECT d.id, d.date, ch.nom AS chantier, d.categorie, d.description, d.montant "
            "FROM depenses d LEFT JOIN chantiers ch ON ch.id = d.chantier_id ORDER BY d.date DESC, d.id DESC"
        )
        if not df.empty:
            if f_chantier:
                df = df[df["chantier"] == chantiers[f_chantier]]
            if f_cat:
                df = df[df["categorie"].isin(f_cat)]
            if isinstance(f_periode, tuple) and len(f_periode) == 2:
                df = df[(df["date"] >= f_periode[0].isoformat()) & (df["date"] <= f_periode[1].isoformat())]
        st.dataframe(df, hide_index=True, use_container_width=True)
        st.metric("Total affiché", fcfa(df["montant"].sum() if not df.empty else 0))
        if is_admin() and not df.empty:
            st.divider()
            ids = list(df["id"].astype(int))
            did = st.selectbox("Dépense à supprimer (n°)", ids, key="dep_del_sel")
            if confirm_delete(f"dep_{did}") and st.button("Supprimer la dépense", key="dep_del_btn"):
                if db_write("DELETE FROM depenses WHERE id = ?", (did,)) is not None:
                    flash("Dépense supprimée.")
                    st.rerun()

    with tab_add:
        if not can_edit():
            read_only_notice()
        elif not chantiers:
            st.info("Créez d'abord un chantier.")
        else:
            with st.form("dep_add", clear_on_submit=True):
                cid = st.selectbox("Chantier *", list(chantiers), format_func=chantiers.get)
                cat = st.selectbox("Catégorie", CATEGORIES_DEPENSE)
                montant = st.number_input(f"Montant ({DEVISE}) *", min_value=0.0, step=1000.0, format="%.0f")
                desc = st.text_input("Description")
                when = st.date_input("Date", value=date.today())
                if st.form_submit_button("Enregistrer", type="primary"):
                    if montant <= 0:
                        st.error("Le montant doit être supérieur à zéro.")
                    elif db_write(
                        "INSERT INTO depenses (chantier_id, categorie, description, montant, date) VALUES (?,?,?,?,?)",
                        (cid, cat, desc.strip(), montant, when.isoformat()),
                    ) is not None:
                        flash("Dépense enregistrée.")
                        st.rerun()


# --------------------------------------------------------------------------- #
# Rapports et exports
# --------------------------------------------------------------------------- #
def page_rapports() -> None:
    st.header("📑 Rapports et exports")
    synthese = query(
        """
        SELECT ch.nom AS chantier, ch.statut, ch.avancement AS avancement_pct, ch.budget,
               COALESCE((SELECT SUM(montant) FROM depenses WHERE chantier_id = ch.id), 0) AS depenses,
               COALESCE((SELECT SUM(p.heures / 8.0 * e.taux_journalier) FROM pointages p
                         JOIN employes e ON e.id = p.employe_id WHERE p.chantier_id = ch.id), 0) AS main_oeuvre
        FROM chantiers ch ORDER BY ch.nom
        """
    )
    if synthese.empty:
        st.info("Aucune donnée à présenter pour le moment.")
    else:
        synthese["reste_budget"] = synthese["budget"] - synthese["depenses"]
        synthese["consommation_pct"] = (
            (synthese["depenses"] / synthese["budget"].where(synthese["budget"] > 0)) * 100
        ).round(1)
        st.subheader("Synthèse par chantier")
        st.dataframe(synthese, hide_index=True, use_container_width=True)

    st.subheader("Exports")
    tables = {
        "Clients": "SELECT * FROM clients",
        "Chantiers": "SELECT * FROM chantiers",
        "Depenses": "SELECT * FROM depenses",
        "Materiaux": "SELECT * FROM materiaux",
        "Mouvements": "SELECT * FROM mouvements_stock",
        "Employes": "SELECT * FROM employes",
        "Pointages": "SELECT * FROM pointages",
        "Devis": "SELECT * FROM devis",
        "Lignes_devis": "SELECT * FROM devis_lignes",
        "Intervenants": (
            "SELECT ch.nom AS chantier, e.nom AS employe, a.fonction, e.telephone, e.taux_journalier "
            "FROM affectations a JOIN chantiers ch ON ch.id = a.chantier_id "
            "JOIN employes e ON e.id = a.employe_id ORDER BY ch.nom, a.fonction, e.nom"
        ),
        "Appels_offres": "SELECT * FROM appels_offres",
        "Offres_concurrents": "SELECT * FROM concurrents_offres",
        "Prix_marche": "SELECT * FROM prix_marche",
    }
    if st.button("Préparer le classeur Excel complet"):
        sheets = {"Synthese": synthese} if not synthese.empty else {}
        sheets.update({name: query(sql) for name, sql in tables.items()})
        st.session_state["export_xlsx"] = excel_bytes(sheets)
    if "export_xlsx" in st.session_state:
        st.download_button(
            "⬇️ Télécharger l'export Excel",
            data=st.session_state["export_xlsx"],
            file_name=f"innovation_btp_{date.today().isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    if not synthese.empty:
        st.download_button(
            "⬇️ Synthèse (CSV)",
            data=synthese.to_csv(index=False).encode("utf-8-sig"),
            file_name="synthese_chantiers.csv",
            mime="text/csv",
        )


# --------------------------------------------------------------------------- #
# Paramètres et administration
# --------------------------------------------------------------------------- #
def page_parametres() -> None:
    st.header("⚙️ Paramètres")
    user = current_user()
    assert user is not None
    st.subheader("Changer mon mot de passe")
    with st.form("pwd_change", clear_on_submit=True):
        old = st.text_input("Mot de passe actuel", type="password")
        new = st.text_input("Nouveau mot de passe", type="password")
        again = st.text_input("Confirmer le nouveau mot de passe", type="password")
        if st.form_submit_button("Modifier", type="primary"):
            row = query("SELECT * FROM users WHERE id = ?", (user["id"],)).iloc[0]
            problems = password_problems(new)
            if not verify_password(old, row["password_hash"], row["salt"]):
                st.error("Mot de passe actuel incorrect.")
            elif new != again:
                st.error("Les deux mots de passe ne correspondent pas.")
            elif problems:
                st.error("Le mot de passe doit contenir : " + ", ".join(problems) + ".")
            else:
                pwd_hash, salt = hash_password(new)
                if db_write("UPDATE users SET password_hash=?, salt=? WHERE id=?", (pwd_hash, salt, user["id"])) is not None:
                    flash("Mot de passe modifié.")
                    st.rerun()

    if not is_admin():
        return

    st.divider()
    st.subheader("Utilisateurs")
    users = query("SELECT id, username, role, created_at FROM users ORDER BY username")
    st.dataframe(users, hide_index=True, use_container_width=True)

    with st.form("user_add", clear_on_submit=True):
        st.write("**Ajouter un utilisateur**")
        username = st.text_input("Nom d'utilisateur")
        password = st.text_input("Mot de passe initial", type="password")
        role = st.selectbox("Rôle", ROLES, index=1)
        if st.form_submit_button("Créer l'utilisateur"):
            problems = password_problems(password)
            if not re.fullmatch(r"[A-Za-z0-9_.-]{3,30}", username.strip()):
                st.error("Nom d'utilisateur : 3 à 30 caractères (lettres, chiffres, _ . -).")
            elif problems:
                st.error("Le mot de passe doit contenir : " + ", ".join(problems) + ".")
            else:
                pwd_hash, salt = hash_password(password)
                if db_write(
                    "INSERT INTO users (username, password_hash, salt, role, created_at) VALUES (?,?,?,?,?)",
                    (username.strip(), pwd_hash, salt, role, datetime.now().isoformat(timespec="seconds")),
                ) is not None:
                    flash("Utilisateur créé.")
                    st.rerun()

    others = {int(r["id"]): f"{r['username']} ({r['role']})" for _, r in users.iterrows() if int(r["id"]) != user["id"]}
    if others:
        uid = st.selectbox("Gérer un utilisateur", list(others), format_func=others.get, key="user_manage")
        col1, col2 = st.columns(2)
        new_role = col1.selectbox("Nouveau rôle", ROLES, key="user_new_role")
        if col1.button("Changer le rôle"):
            if db_write("UPDATE users SET role = ? WHERE id = ?", (new_role, uid)) is not None:
                flash("Rôle mis à jour.")
                st.rerun()
        if confirm_delete(f"user_{uid}") and col2.button("Supprimer l'utilisateur"):
            if db_write("DELETE FROM users WHERE id = ?", (uid,)) is not None:
                flash("Utilisateur supprimé.")
                st.rerun()

    st.divider()
    st.subheader("Sauvegarde")
    st.caption(
        "Sur Streamlit Cloud, le disque est temporaire : téléchargez régulièrement une sauvegarde, "
        "ou utilisez une base externe (PostgreSQL, Supabase) pour une conservation durable."
    )
    try:
        data = get_connection().serialize()
        st.download_button(
            "⬇️ Télécharger la base (SQLite)",
            data=bytes(data),
            file_name=f"innovation_btp_{date.today().isoformat()}.db",
            mime="application/octet-stream",
        )
    except (AttributeError, sqlite3.Error):
        if DB_PATH.exists():
            st.download_button("⬇️ Télécharger la base (SQLite)", data=DB_PATH.read_bytes(), file_name=DB_PATH.name)


# --------------------------------------------------------------------------- #
# Navigation principale
# --------------------------------------------------------------------------- #
PAGES = {
    "📊 Tableau de bord": page_dashboard,
    "🏗️ Chantiers": page_chantiers,
    "🧑‍🔧 Intervenants": page_intervenants,
    "👥 Clients": page_clients,
    "🧾 Devis": page_devis,
    "📈 Marchés BTP": page_marches,
    "📦 Stock": page_stock,
    "👷 Personnel": page_personnel,
    "💸 Dépenses": page_depenses,
    "📑 Rapports": page_rapports,
    "⚙️ Paramètres": page_parametres,
}


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="🏗️", layout="wide")
    try:
        get_connection()
        ensure_schema()
    except sqlite3.Error as exc:
        st.error(f"Impossible d'ouvrir la base de données : {exc}")
        st.stop()

    ensure_demo_data()
    if not current_user():
        login_page()
        return

    show_flash()
    user = current_user()
    with st.sidebar:
        st.title(f"🏗️ {APP_TITLE}")
        st.caption(f"Connecté : **{user['username']}** ({user['role']})")
        page = st.radio("Navigation", list(PAGES), label_visibility="collapsed")
        st.divider()
        if st.button("Se déconnecter"):
            logout()

    theme = BANNER_KEYS.get(page.split(' ', 1)[-1])
    if theme:
        render_banner(theme)
    try:
        PAGES[page]()
    except Exception as exc:  # noqa: BLE001  - filet de sécurité : ne jamais afficher une page blanche
        log.exception("Erreur dans la page %s", page)
        st.error(f"Une erreur inattendue est survenue : {exc}")


main()
