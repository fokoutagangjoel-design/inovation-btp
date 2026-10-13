"""
BTP Paie Pro - application 100 % Python (interface Flet)

Remplace entièrement l'ancienne page HTML/JavaScript :
  - Interface    : Flet (Python -> application Android / iOS / Windows / Mac / Linux / web)
  - Données      : SQLite local (hors-ligne)  OU  Supabase (cloud, même base que l'ancienne app)
  - Reçus        : PDF générés en Python (reportlab)
  - Export       : CSV

Lancer :  pip install -r requirements.txt   puis   python main.py
"""
import csv
import os
import sqlite3
import subprocess
import sys
import webbrowser
from datetime import date, timedelta
from pathlib import Path

try:
    import flet as ft
except ImportError:  # permet de tester la logique sans Flet
    ft = None

# ------------------------------------------------------------------ CONFIG
SUPABASE_URL = os.environ.get("BTP_SUPABASE_URL", "https://mvcprsbftcppchnnriqm.supabase.co")
SUPABASE_KEY = os.environ.get(
    "BTP_SUPABASE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im12Y3Byc2JmdGNwcGNobm5yaXFtIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTExOTI5MTAsImV4cCI6MjEwNjc2ODkxMH0.Jxb4laRt-L3VarlmvCVbjWb1swLyzxKwINRgpbqo61o",
)
DATA_DIR = Path(os.environ.get("FLET_APP_STORAGE_DATA") or (Path.home() / "BTP_Paie"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
RECUS_DIR = DATA_DIR / "recus"
RECUS_DIR.mkdir(exist_ok=True)

NOIR, BLANC, GRIS, FOND = "#000000", "#ffffff", "#888888", "#f2f2f3"
ROUGE, VERT, ORANGE = "#d62828", "#2a9d3f", "#f4a100"


def today():
    return date.today().isoformat()


def fcfa(n):
    return f"{float(n):,.0f}".replace(",", " ") + " F"


def nombre(texte):
    """Convertit un champ texte en nombre positif, sinon None."""
    try:
        v = float(str(texte).replace(" ", "").replace(",", "."))
        return v if v > 0 else None
    except ValueError:
        return None


# ------------------------------------------------------------------ CALCULS
def calculer(ouvriers, presences, avances, paiements, caisse):
    """Dû = jours x salaire ; reste = dû - avances - paiements (comme l'app d'origine)."""
    lignes = []
    for o in ouvriers:
        oid = o["id"]
        jours = sum(1 for p in presences if p["ouvrier_id"] == oid)
        du = jours * float(o["salaire_journalier"] or 0)
        av = sum(float(a["montant"]) for a in avances if a["ouvrier_id"] == oid)
        pa = sum(float(p["montant"]) for p in paiements if p["ouvrier_id"] == oid)
        lignes.append(dict(id=oid, nom=o["nom"], poste=o.get("poste") or "", actif=bool(o.get("actif", 1)),
                           salaire=float(o["salaire_journalier"] or 0), jours=jours,
                           du=du, avances=av, paye=pa, reste=du - av - pa))
    solde = sum(float(c["montant"]) * (1 if c["type"] == "entree" else -1) for c in caisse)
    kpi = dict(actifs=sum(1 for l in lignes if l["actif"]),
               jours=sum(l["jours"] for l in lignes),
               avances=sum(l["avances"] for l in lignes),
               reste=sum(l["reste"] for l in lignes),
               caisse=solde)
    return lignes, kpi


# ------------------------------------------------------------------ STOCKAGE LOCAL (SQLite)
SCHEMA = """
CREATE TABLE IF NOT EXISTS chantiers(id INTEGER PRIMARY KEY, nom TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ouvriers(id INTEGER PRIMARY KEY, nom TEXT NOT NULL, poste TEXT,
    salaire_journalier REAL NOT NULL, actif INTEGER DEFAULT 1, chantier_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS presences(id INTEGER PRIMARY KEY, ouvrier_id INTEGER NOT NULL,
    date TEXT NOT NULL, chantier_id INTEGER NOT NULL, UNIQUE(ouvrier_id, date));
CREATE TABLE IF NOT EXISTS avances(id INTEGER PRIMARY KEY, ouvrier_id INTEGER NOT NULL,
    montant REAL NOT NULL, motif TEXT, date TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paiements(id INTEGER PRIMARY KEY, ouvrier_id INTEGER NOT NULL,
    montant REAL NOT NULL, periode TEXT, date TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS caisse(id INTEGER PRIMARY KEY, type TEXT NOT NULL, montant REAL NOT NULL,
    description TEXT, date TEXT NOT NULL, chantier_id INTEGER NOT NULL);
"""


class LocalStore:
    nom = "Mode local (hors-ligne)"

    def __init__(self, chemin):
        self.c = sqlite3.connect(str(chemin), check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)

    def _all(self, sql, *a):
        return [dict(r) for r in self.c.execute(sql, a)]

    def _ins(self, sql, *a):
        cur = self.c.execute(sql, a)
        self.c.commit()
        return cur.lastrowid

    def chantiers(self):
        return self._all("SELECT * FROM chantiers ORDER BY id")

    def add_chantier(self, nom):
        return self._ins("INSERT INTO chantiers(nom) VALUES(?)", nom)

    def ouvriers(self, cid):
        return self._all("SELECT * FROM ouvriers WHERE chantier_id=? ORDER BY nom", cid)

    def add_ouvrier(self, cid, nom, poste, salaire, actif=True):
        return self._ins("INSERT INTO ouvriers(nom,poste,salaire_journalier,actif,chantier_id) VALUES(?,?,?,?,?)",
                         nom, poste, salaire, int(actif), cid)

    def del_ouvrier(self, oid):
        for t in ("presences", "avances", "paiements"):
            self.c.execute(f"DELETE FROM {t} WHERE ouvrier_id=?", (oid,))
        self.c.execute("DELETE FROM ouvriers WHERE id=?", (oid,))
        self.c.commit()

    def presences(self, cid):
        return self._all("SELECT * FROM presences WHERE chantier_id=?", cid)

    def add_presences(self, cid, ouvrier_ids, jour):
        for oid in ouvrier_ids:
            self.c.execute("INSERT OR IGNORE INTO presences(ouvrier_id,date,chantier_id) VALUES(?,?,?)",
                           (oid, jour, cid))
        self.c.commit()

    def avances(self, cid):
        return self._all("""SELECT a.* FROM avances a JOIN ouvriers o ON o.id=a.ouvrier_id
                            WHERE o.chantier_id=? ORDER BY a.date DESC, a.id DESC""", cid)

    def add_avance(self, oid, montant, motif, jour=None):
        return self._ins("INSERT INTO avances(ouvrier_id,montant,motif,date) VALUES(?,?,?,?)",
                         oid, montant, motif, jour or today())

    def paiements(self, cid):
        return self._all("""SELECT p.* FROM paiements p JOIN ouvriers o ON o.id=p.ouvrier_id
                            WHERE o.chantier_id=? ORDER BY p.date DESC, p.id DESC""", cid)

    def add_paiement(self, oid, montant, periode, jour=None):
        return self._ins("INSERT INTO paiements(ouvrier_id,montant,periode,date) VALUES(?,?,?,?)",
                         oid, montant, periode, jour or today())

    def caisse(self, cid):
        return self._all("SELECT * FROM caisse WHERE chantier_id=? ORDER BY date DESC, id DESC", cid)

    def add_caisse(self, cid, type_, montant, description, jour=None):
        return self._ins("INSERT INTO caisse(type,montant,description,date,chantier_id) VALUES(?,?,?,?,?)",
                         type_, montant, description, jour or today(), cid)


# ------------------------------------------------------------------ STOCKAGE CLOUD (Supabase)
class SupaStore:
    """Même base et mêmes tables que l'ancienne application web."""
    nom = "Cloud Supabase"

    def __init__(self, url, cle):
        from supabase import create_client  # pip install supabase
        self.sb = create_client(url, cle)
        self.uid = None

    def login(self, email, mdp):
        try:
            r = self.sb.auth.sign_in_with_password({"email": email, "password": mdp})
        except Exception:
            self.sb.auth.sign_up({"email": email, "password": mdp})
            raise RuntimeError("Compte créé. Reconnecte-toi maintenant (confirme l'email si demandé).")
        self.uid = r.user.id

    def _sel(self, table, **eq):
        q = self.sb.table(table).select("*")
        for k, v in eq.items():
            q = q.eq(k, v)
        return q.execute().data or []

    def chantiers(self):
        return self.sb.table("chantiers").select("*").order("created_at").execute().data or []

    def add_chantier(self, nom):
        return self.sb.table("chantiers").insert({"nom": nom, "user_id": self.uid}).execute().data[0]["id"]

    def ouvriers(self, cid):
        return sorted(self._sel("ouvriers", chantier_id=cid), key=lambda o: o["nom"])

    def add_ouvrier(self, cid, nom, poste, salaire, actif=True):
        return self.sb.table("ouvriers").insert(
            {"nom": nom, "poste": poste, "salaire_journalier": salaire, "actif": actif,
             "chantier_id": cid, "user_id": self.uid}).execute().data[0]["id"]

    def del_ouvrier(self, oid):
        self.sb.table("ouvriers").delete().eq("id", oid).execute()

    def presences(self, cid):
        return self._sel("presences", chantier_id=cid)

    def add_presences(self, cid, ouvrier_ids, jour):
        deja = {p["ouvrier_id"] for p in self._sel("presences", chantier_id=cid) if p["date"] == jour}
        lignes = [{"ouvrier_id": o, "date": jour, "chantier_id": cid, "user_id": self.uid}
                  for o in ouvrier_ids if o not in deja]
        if lignes:
            self.sb.table("presences").insert(lignes).execute()

    def _par_ouvriers(self, table, cid):
        ids = [o["id"] for o in self.ouvriers(cid)]
        if not ids:
            return []
        rows = self.sb.table(table).select("*").in_("ouvrier_id", ids).execute().data or []
        return sorted(rows, key=lambda r: r["date"], reverse=True)

    def avances(self, cid):
        return self._par_ouvriers("avances", cid)

    def add_avance(self, oid, montant, motif, jour=None):
        return self.sb.table("avances").insert(
            {"ouvrier_id": oid, "montant": montant, "motif": motif, "date": jour or today(),
             "user_id": self.uid}).execute().data[0]["id"]

    def paiements(self, cid):
        rows = self._par_ouvriers("paiements", cid)
        for r in rows:
            r["periode"] = r.get("periode_debut") or ""
        return rows

    def add_paiement(self, oid, montant, periode, jour=None):
        return self.sb.table("paiements").insert(
            {"ouvrier_id": oid, "montant": montant, "periode_debut": periode, "date": jour or today(),
             "user_id": self.uid}).execute().data[0]["id"]

    def caisse(self, cid):
        rows = self._sel("caisse", chantier_id=cid)
        return sorted(rows, key=lambda r: r["date"], reverse=True)

    def add_caisse(self, cid, type_, montant, description, jour=None):
        return self.sb.table("caisse").insert(
            {"type": type_, "montant": montant, "description": description, "date": jour or today(),
             "chantier_id": cid, "user_id": self.uid}).execute().data[0]["id"]


# ------------------------------------------------------------------ DONNÉES D'EXEMPLE
def charger_exemple(store):
    """Crée un chantier de démonstration avec ouvriers, présences, avances, paiement et caisse."""
    cid = store.add_chantier("Villa Cocody")
    modele = [("Moussa Traoré", "Maçon", 7000, True, [0, 1, 2, 3, 4, 5]),
              ("Ibrahim Koné", "Ferrailleur", 6500, True, [0, 1, 2, 4, 5]),
              ("Adama Diallo", "Coffreur", 6000, True, [0, 1, 3, 4, 5, 6]),
              ("Seydou Ouattara", "Manœuvre", 4000, True, [1, 2, 3, 4]),
              ("Karim Sanogo", "Électricien", 8000, False, [0, 1])]
    debut = date.today() - timedelta(days=6)
    ids = {}
    for nom, poste, sal, actif, jours in modele:
        ids[nom] = store.add_ouvrier(cid, nom, poste, sal, actif)
        for j in jours:
            store.add_presences(cid, [ids[nom]], (debut + timedelta(days=j)).isoformat())
    store.add_avance(ids["Moussa Traoré"], 10000, "Transport")
    store.add_avance(ids["Ibrahim Koné"], 5000, "Santé")
    store.add_avance(ids["Seydou Ouattara"], 3000, "Nourriture")
    store.add_paiement(ids["Adama Diallo"], 20000, "Semaine 1")
    store.add_caisse(cid, "entree", 500000, "Dotation chef de chantier")
    store.add_caisse(cid, "sortie", 35000, "Achat ciment (5 sacs)")
    store.add_caisse(cid, "sortie", 18000, "Location bétonnière")
    store.add_caisse(cid, "sortie", 20000, "Paiement Adama Diallo")
    return cid


# ------------------------------------------------------------------ FICHIERS (PDF / CSV)
def nom_fichier(texte):
    return "".join(c if c.isalnum() else "_" for c in texte).strip("_") or "fichier"


def generer_recu_pdf(paiement, ouvrier_nom, poste, chantier_nom):
    """Reçu de paiement en PDF (équivalent de html2pdf dans l'ancienne app)."""
    from reportlab.lib.pagesizes import A5
    from reportlab.pdfgen import canvas

    chemin = RECUS_DIR / f"recu_{paiement['id']}_{nom_fichier(ouvrier_nom)}.pdf"
    c = canvas.Canvas(str(chemin), pagesize=A5)
    w, h = A5
    c.setLineWidth(2)
    c.roundRect(25, 25, w - 50, h - 50, 12)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(45, h - 70, f"Reçu de paiement n° {paiement['id']}")
    lignes = [("Chantier", chantier_nom), ("Ouvrier", f"{ouvrier_nom} ({poste})"),
              ("Montant", fcfa(paiement["montant"])), ("Période", paiement.get("periode") or "-"),
              ("Date", paiement["date"])]
    y = h - 115
    for etiquette, valeur in lignes:
        c.setFont("Helvetica", 11)
        c.drawString(45, y, etiquette)
        c.setFont("Helvetica-Bold", 11)
        c.drawRightString(w - 45, y, str(valeur))
        c.line(45, y - 8, w - 45, y - 8)
        y -= 32
    c.setFont("Helvetica", 10)
    c.drawString(45, 70, "Signature ouvrier")
    c.drawRightString(w - 45, 70, "Signature responsable")
    c.save()
    return chemin


def exporter_csv(lignes, chantier_nom):
    chemin = DATA_DIR / f"rapport_paie_{nom_fichier(chantier_nom)}.csv"
    with open(chemin, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Ouvrier", "Poste", "Jours", "Salaire/j", "Dû", "Avances", "Payé", "Reste"])
        for l in lignes:
            w.writerow([l["nom"], l["poste"], l["jours"], l["salaire"], l["du"],
                        l["avances"], l["paye"], l["reste"]])
    return chemin


def ouvrir_fichier(chemin):
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(chemin))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(chemin)])
        else:
            subprocess.Popen(["xdg-open", str(chemin)])
    except Exception:
        try:
            webbrowser.open(Path(chemin).as_uri())
        except Exception:
            pass


# ------------------------------------------------------------------ APPLICATION
ONGLETS = [("dash", "📊 Dash"), ("ouvriers", "👷 Ouvriers"), ("presences", "📅 Présences"),
           ("avances", "💸 Avances"), ("paiements", "🧾 Paiements"), ("caisse", "💰 Caisse")]


class App:
    def __init__(self, page):
        self.page = page
        self.store = None
        self.cid = None
        self.tab = "dash"
        self.chantiers, self.ouvriers = [], []
        self.presences, self.avances, self.paiements, self.caisse = [], [], [], []
        self.lignes, self.kpi = [], {}

    # ---------- données
    def reload(self):
        s = self.store
        self.chantiers = s.chantiers()
        ids = [c["id"] for c in self.chantiers]
        if self.cid not in ids:
            self.cid = ids[0] if ids else None
        if self.cid is None:
            self.ouvriers = self.presences = self.avances = self.paiements = self.caisse = []
        else:
            self.ouvriers = s.ouvriers(self.cid)
            self.presences = s.presences(self.cid)
            self.avances = s.avances(self.cid)
            self.paiements = s.paiements(self.cid)
            self.caisse = s.caisse(self.cid)
        self.lignes, self.kpi = calculer(self.ouvriers, self.presences, self.avances,
                                         self.paiements, self.caisse)

    def chantier_nom(self):
        return next((c["nom"] for c in self.chantiers if c["id"] == self.cid), "")

    def ouvrier(self, cle):
        return next((o for o in self.ouvriers if str(o["id"]) == str(cle)), None)

    def nom_ouvrier(self, oid):
        o = self.ouvrier(oid)
        return o["nom"] if o else "?"

    # ---------- actions (logique séparée de l'interface)
    def ajouter_chantier(self, nom):
        if not nom.strip():
            return "Nom du chantier requis"
        self.cid = self.store.add_chantier(nom.strip())
        self.reload()

    def ajouter_ouvrier(self, nom, poste, salaire):
        s = nombre(salaire)
        if not nom.strip() or s is None:
            return "Nom et salaire requis"
        self.store.add_ouvrier(self.cid, nom.strip(), poste.strip(), s)
        self.reload()

    def supprimer_ouvrier(self, oid):
        self.store.del_ouvrier(oid)
        self.reload()

    def enregistrer_presences(self, ouvrier_ids, jour):
        try:
            date.fromisoformat(jour)
        except ValueError:
            return "Date invalide (format AAAA-MM-JJ)"
        self.store.add_presences(self.cid, ouvrier_ids, jour)
        self.reload()

    def ajouter_avance(self, cle, montant, motif):
        m, o = nombre(montant), self.ouvrier(cle)
        if m is None or o is None:
            return "Ouvrier et montant requis"
        self.store.add_avance(o["id"], m, motif.strip())
        self.reload()

    def ajouter_paiement(self, cle, montant, periode, deduire_caisse):
        """Retourne (erreur, chemin_du_recu_pdf)."""
        m, o = nombre(montant), self.ouvrier(cle)
        if m is None or o is None:
            return "Ouvrier et montant requis", None
        pid = self.store.add_paiement(o["id"], m, periode.strip())
        if deduire_caisse:
            self.store.add_caisse(self.cid, "sortie", m, f"Paiement {o['nom']}")
        self.reload()
        return None, self.recu(pid)

    def recu(self, pid):
        p = next(x for x in self.paiements if str(x["id"]) == str(pid))
        o = self.ouvrier(p["ouvrier_id"]) or {"nom": "?", "poste": ""}
        return generer_recu_pdf(p, o["nom"], o.get("poste") or "", self.chantier_nom())

    def ajouter_caisse(self, type_, montant, description):
        m = nombre(montant)
        if m is None:
            return "Montant requis"
        self.store.add_caisse(self.cid, type_, m, description.strip())
        self.reload()

    # ---------- petits composants d'interface
    def toast(self, message):
        self.page.open(ft.SnackBar(ft.Text(message)))

    def erreur_ou(self, err, apres=None):
        """Affiche l'erreur éventuelle, sinon rafraîchit l'écran."""
        if err:
            self.toast(err)
        else:
            if apres:
                apres()
            self.draw()

    def t(self, valeur, size=14, gras=False, couleur=None):
        return ft.Text(str(valeur), size=size, color=couleur,
                       weight=ft.FontWeight.BOLD if gras else None)

    def carte(self, contenu):
        return ft.Container(content=contenu, bgcolor=BLANC, border_radius=20, padding=16,
                            margin=ft.margin.only(bottom=12))

    def champ(self, label, nombre_=False, valeur=""):
        return ft.TextField(label=label, value=valeur, border_radius=12, filled=True, bgcolor=FOND,
                            border_color=FOND,
                            keyboard_type=ft.KeyboardType.NUMBER if nombre_ else ft.KeyboardType.TEXT)

    def liste_deroulante(self, label, options):
        return ft.Dropdown(label=label, border_radius=12, filled=True, bgcolor=FOND, border_color=FOND,
                           options=[ft.dropdown.Option(key=str(k), text=txt) for k, txt in options],
                           value=str(options[0][0]) if options else None)

    def bouton(self, label, action, fond=NOIR, texte=BLANC):
        return ft.Row([ft.ElevatedButton(
            label, on_click=action, height=48, expand=True,
            style=ft.ButtonStyle(bgcolor=fond, color=texte,
                                 shape=ft.RoundedRectangleBorder(radius=12)))])

    def ligne(self, gauche, droite):
        return ft.Container(
            padding=ft.padding.symmetric(vertical=10),
            border=ft.border.only(bottom=ft.BorderSide(1, "#eeeeee")),
            content=ft.Row([ft.Column(gauche, spacing=2, expand=True),
                            ft.Column(droite, spacing=2, horizontal_alignment=ft.CrossAxisAlignment.END)],
                           alignment=ft.MainAxisAlignment.SPACE_BETWEEN))

    # ---------- écrans
    def start(self):
        p = self.page
        p.title = "BTP Paie Pro"
        p.padding = 0
        p.scroll = ft.ScrollMode.AUTO
        self.ecran_connexion()

    def ecran_connexion(self):
        p = self.page
        p.clean()
        p.bgcolor = NOIR
        email, mdp = self.champ("Email chantier"), self.champ("Mot de passe")
        mdp.password, mdp.can_reveal_password = True, True
        msg = ft.Text("", color=ROUGE, size=12)

        def cloud(e):
            try:
                store = SupaStore(SUPABASE_URL, SUPABASE_KEY)
                store.login(email.value.strip(), mdp.value.strip())
                self.store = store
                self.entrer()
            except ImportError:
                msg.value = "Installe le module cloud :  pip install supabase"
                p.update()
            except Exception as ex:
                msg.value = str(ex)
                p.update()

        def local(e):
            self.store = LocalStore(DATA_DIR / "btp_paie.db")
            self.entrer()

        p.add(ft.Container(
            padding=24, alignment=ft.alignment.center, height=700,
            content=ft.Container(
                bgcolor=BLANC, border_radius=28, padding=28, width=380,
                content=ft.Column([
                    ft.Container(ft.Text("B", size=20, weight=ft.FontWeight.BOLD, color=BLANC),
                                 bgcolor=NOIR, width=48, height=48, border_radius=12,
                                 alignment=ft.alignment.center),
                    self.t("BTP Paie Pro", 24, True),
                    self.t("Gestion de paie de chantier", 13, couleur=GRIS),
                    email, mdp,
                    self.bouton("Se connecter / Créer compte (cloud)", cloud),
                    self.bouton("Utiliser en mode local (hors-ligne)", local, fond=FOND, texte=NOIR),
                    msg], spacing=12))))
        p.update()

    def entrer(self):
        self.page.bgcolor = FOND
        self.reload()
        self.draw()

    def draw(self):
        p = self.page
        p.clean()
        corps = [self.entete()]
        if self.cid is None:
            corps.append(ft.Container(padding=16, content=self.carte(ft.Column([
                self.t("Aucun chantier pour l'instant. Crées-en un ci-dessus, ou :"),
                self.bouton("Charger les données d'exemple", self.action_exemple)]))))
        else:
            corps += [self.bloc_kpi(), self.barre_onglets(),
                      ft.Container(padding=16, content=ft.Column(getattr(self, "vue_" + self.tab)()))]
        p.add(*corps)
        p.update()

    def action_exemple(self, e=None):
        self.cid = charger_exemple(self.store)
        self.reload()
        self.draw()

    def entete(self):
        sel = ft.Dropdown(
            value=str(self.cid) if self.cid is not None else None, width=220, color=BLANC,
            border_color="#333333", filled=True, bgcolor="#222222",
            options=[ft.dropdown.Option(key=str(c["id"]), text=c["nom"]) for c in self.chantiers])

        def changer(e):
            self.cid = next(c["id"] for c in self.chantiers if str(c["id"]) == sel.value)
            self.reload()
            self.draw()

        sel.on_change = changer
        nouveau = ft.TextField(hint_text="Nouveau chantier", expand=True, height=44, color=BLANC,
                               border_radius=20, filled=True, bgcolor="#222222", border_color="#222222",
                               text_size=14, content_padding=ft.padding.symmetric(horizontal=16))

        def ajouter(e):
            self.erreur_ou(self.ajouter_chantier(nouveau.value or ""))

        def sortir(e):
            self.store, self.cid, self.tab = None, None, "dash"
            self.ecran_connexion()

        return ft.Container(
            bgcolor=NOIR, padding=ft.padding.only(left=20, right=20, top=40, bottom=28),
            border_radius=ft.border_radius.only(bottom_left=28, bottom_right=28),
            content=ft.Column([
                ft.Row([ft.Column([self.t("CHANTIER ACTIF", 11, couleur="#999999"), sel], spacing=4),
                        ft.TextButton("Sortir", on_click=sortir)],
                       alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                self.t(self.store.nom, 11, couleur="#999999"),
                ft.Row([nouveau, ft.ElevatedButton("+", on_click=ajouter)])]))

    def bloc_kpi(self):
        def tuile(titre, valeur, couleur=NOIR):
            return ft.Container(col=6, bgcolor=BLANC, border_radius=20, padding=16,
                                content=ft.Column([self.t(titre, 11, couleur=GRIS),
                                                   self.t(valeur, 20, True, couleur)], spacing=2))
        k = self.kpi
        return ft.Container(
            padding=ft.padding.only(left=16, right=16, top=12),
            content=ft.ResponsiveRow([
                tuile("ACTIFS", k["actifs"]), tuile("JOURS", k["jours"]),
                tuile("AVANCES", fcfa(k["avances"]), ORANGE), tuile("RESTE À PAYER", fcfa(k["reste"]), ROUGE),
                ft.Container(col=12, bgcolor=NOIR, border_radius=20, padding=16,
                             content=ft.Row([self.t("💰 Solde caisse", 14, couleur=BLANC),
                                             self.t(fcfa(k["caisse"]), 16, True, BLANC)],
                                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN))]))

    def barre_onglets(self):
        def aller(cle):
            def f(e):
                self.tab = cle
                self.draw()
            return f

        return ft.Container(
            padding=ft.padding.only(left=16, right=16, top=12),
            content=ft.Row([ft.ElevatedButton(
                nom, on_click=aller(cle),
                style=ft.ButtonStyle(bgcolor=NOIR if cle == self.tab else BLANC,
                                     color=BLANC if cle == self.tab else NOIR))
                for cle, nom in ONGLETS], scroll=ft.ScrollMode.AUTO))

    # ---------- onglets (chaque vue_* retourne une liste de composants)
    def vue_dash(self):
        def exporter(e):
            chemin = exporter_csv(self.lignes, self.chantier_nom())
            self.toast(f"CSV enregistré : {chemin}")
            ouvrir_fichier(chemin)

        lignes = [self.ligne(
            [self.t(l["nom"] + ("" if l["actif"] else " (inactif)"), 14, True),
             self.t(f"{l['jours']}j × {l['salaire']:.0f} = {fcfa(l['du'])}", 11, couleur=GRIS)],
            [self.t(f"Av {fcfa(l['avances'])} · Payé {fcfa(l['paye'])}", 11, couleur=ORANGE),
             self.t(f"{fcfa(l['reste'])} dû", 14, True, ROUGE if l["reste"] > 0 else VERT)])
            for l in self.lignes] or [self.t("Aucun ouvrier.", couleur=GRIS)]
        return [self.carte(ft.Column([self.t("📈 Détail par ouvrier", 16, True), *lignes])),
                self.bouton("⬇ Exporter en CSV", exporter, fond=FOND, texte=NOIR)]

    def vue_ouvriers(self):
        nom, poste = self.champ("Nom ouvrier"), self.champ("Poste")
        salaire = self.champ("Salaire / jour FCFA", True)

        def ajouter(e):
            self.erreur_ou(self.ajouter_ouvrier(nom.value or "", poste.value or "", salaire.value or ""))

        def supprimer(o):
            def f(e):
                dlg = ft.AlertDialog(title=ft.Text(f"Supprimer {o['nom']} ?"),
                                     content=ft.Text("Ses présences, avances et paiements seront aussi supprimés."))

                def oui(e2):
                    self.page.close(dlg)
                    self.supprimer_ouvrier(o["id"])
                    self.draw()

                dlg.actions = [ft.TextButton("Annuler", on_click=lambda e2: self.page.close(dlg)),
                               ft.TextButton("Supprimer", on_click=oui)]
                self.page.open(dlg)
            return f

        liste = [self.carte(ft.Row([
            ft.Column([self.t(o["nom"], 14, True),
                       self.t(f"{o.get('poste') or ''} • {float(o['salaire_journalier']):.0f} F/j", 11, couleur=GRIS)],
                      spacing=2),
            ft.TextButton("Suppr", on_click=supprimer(o))],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN)) for o in self.ouvriers]
        return [self.carte(ft.Column([nom, poste, salaire, self.bouton("+ Ajouter ouvrier", ajouter)])), *liste]

    def vue_presences(self):
        jour = self.champ("Date (AAAA-MM-JJ)", valeur=today())
        cases = [(o["id"], ft.Checkbox(label=o["nom"], value=True)) for o in self.ouvriers if o.get("actif", 1)]

        def enregistrer(e):
            ids = [oid for oid, c in cases if c.value]
            self.erreur_ou(self.enregistrer_presences(ids, jour.value or ""),
                           apres=lambda: self.toast("Présences enregistrées"))

        return [self.carte(ft.Column([jour, *[c for _, c in cases],
                                      self.bouton("Enregistrer présences", enregistrer)]))]

    def _choix_ouvrier(self):
        return self.liste_deroulante("Ouvrier", [(o["id"], o["nom"]) for o in self.ouvriers])

    def vue_avances(self):
        ouv, montant, motif = self._choix_ouvrier(), self.champ("Montant avance", True), self.champ("Motif")

        def ajouter(e):
            self.erreur_ou(self.ajouter_avance(ouv.value, montant.value or "", motif.value or ""))

        liste = [self.ligne([self.t(self.nom_ouvrier(a["ouvrier_id"]), 14, True),
                             self.t(f"{a['date']} • {a.get('motif') or ''}", 11, couleur=GRIS)],
                            [self.t(fcfa(a["montant"]), 14, True, ORANGE)]) for a in self.avances]
        return [self.carte(ft.Column([ouv, montant, motif,
                                      self.bouton("+ Avance", ajouter, fond=ORANGE)])),
                self.carte(ft.Column(liste or [self.t("Aucune avance.", couleur=GRIS)]))]

    def vue_paiements(self):
        ouv = self.liste_deroulante(
            "Ouvrier", [(l["id"], f"{l['nom']} (reste {fcfa(l['reste'])})") for l in self.lignes])
        montant, periode = self.champ("Montant payé", True), self.champ("Période ex: 01-07 Oct")
        deduire = ft.Checkbox(label="Déduire de la caisse", value=True)

        def dialogue_recu(chemin):
            dlg = ft.AlertDialog(title=ft.Text("Reçu généré"), content=ft.Text(f"PDF enregistré :\n{chemin}"))
            dlg.actions = [ft.TextButton("Fermer", on_click=lambda e: self.page.close(dlg)),
                           ft.TextButton("Ouvrir", on_click=lambda e: ouvrir_fichier(chemin))]
            self.page.open(dlg)

        def payer(e):
            err, chemin = self.ajouter_paiement(ouv.value, montant.value or "", periode.value or "", deduire.value)
            self.erreur_ou(err)
            if chemin:
                dialogue_recu(chemin)

        def revoir(pid):
            return lambda e: dialogue_recu(self.recu(pid))

        liste = [self.ligne([self.t(self.nom_ouvrier(p["ouvrier_id"]), 14, True),
                             self.t(f"{p['date']} • {p.get('periode') or ''}", 11, couleur=GRIS)],
                            [self.t(fcfa(p["montant"]), 14, True),
                             ft.TextButton("Reçu PDF", on_click=revoir(p["id"]))]) for p in self.paiements]
        return [self.carte(ft.Column([ouv, montant, periode, deduire,
                                      self.bouton("Payer & générer reçu PDF", payer)])),
                self.carte(ft.Column(liste or [self.t("Aucun paiement.", couleur=GRIS)]))]

    def vue_caisse(self):
        type_ = self.liste_deroulante("Type", [("entree", "Entrée (+)"), ("sortie", "Sortie (-)")])
        montant, desc = self.champ("Montant", True), self.champ("Description")

        def ajouter(e):
            self.erreur_ou(self.ajouter_caisse(type_.value, montant.value or "", desc.value or ""))

        liste = [self.ligne([self.t(c.get("description") or "", 14, True), self.t(c["date"], 11, couleur=GRIS)],
                            [self.t(("+" if c["type"] == "entree" else "−") + fcfa(c["montant"]), 14, True,
                                    VERT if c["type"] == "entree" else ROUGE)]) for c in self.caisse]
        return [self.carte(ft.Column([type_, montant, desc, self.bouton("Enregistrer", ajouter)])),
                self.carte(ft.Column(liste or [self.t("Aucun mouvement.", couleur=GRIS)]))]


def main(page):
    App(page).start()


if __name__ == "__main__":
    if ft is None:
        sys.exit("Flet n'est pas installé :  pip install -r requirements.txt")
    ft.app(target=main)
