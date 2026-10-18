"""Innovation BTP - application de gestion de chantiers (Streamlit).

Points clés de cette version :
* Aucune utilisation de ``signal.signal`` : l'erreur
  « signal only works in main thread of the main interpreter » ne peut plus venir
  de ce fichier. La connexion à la base est créée UNE seule fois avec
  ``st.cache_resource`` et ``check_same_thread=False`` (compatible Streamlit Cloud).
* Connexion sécurisée (mots de passe hachés PBKDF2, rôles admin / gestionnaire / lecteur).
* Modules : tableau de bord, chantiers, clients, devis, stock, personnel, dépenses,
  rapports/exports, paramètres.

Lancer en local :  streamlit run app.py
"""
from __future__ import annotations

import hashlib
import hmac
import io
import logging
import os
import re
import secrets
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

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


def db_write(sql: str, params: tuple = ()) -> int | None:
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
    st.title(f"🏗️ {APP_TITLE}")
    st.caption("Connectez-vous pour accéder à la gestion de vos chantiers.")
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
# Tableau de bord
# --------------------------------------------------------------------------- #
def page_dashboard() -> None:
    st.header("📊 Tableau de bord")
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
    if not depenses.empty:
        st.write("**Dépenses du chantier**")
        st.dataframe(depenses.drop(columns=["chantier_id"]), hide_index=True, use_container_width=True)
        st.bar_chart(depenses.groupby("categorie")["montant"].sum())


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
    "👥 Clients": page_clients,
    "🧾 Devis": page_devis,
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
    except sqlite3.Error as exc:
        st.error(f"Impossible d'ouvrir la base de données : {exc}")
        st.stop()

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

    try:
        PAGES[page]()
    except Exception as exc:  # noqa: BLE001  - filet de sécurité : ne jamais afficher une page blanche
        log.exception("Erreur dans la page %s", page)
        st.error(f"Une erreur inattendue est survenue : {exc}")


main()
