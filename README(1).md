# Screenlead — collecte, analyse et gestion des données de chantiers

Application web (Python / Streamlit / SQLite) pour suivre plusieurs chantiers de génie civil :
saisie terrain, contrôles qualité, dépenses, incidents, analyses (valeur acquise, qualité, sécurité)
et messagerie entre les acteurs.

## Installation et lancement

```bash
pip install -r requirements.txt
streamlit run app.py
```

Au premier lancement, la base `screenlead.db` est créée avec des **données de démonstration**
(3 chantiers, ~3 mois d'historique).

| Profil | Identifiant | Mot de passe |
|---|---|---|
| Maître d'ouvrage | `moa` | `Moa#2026` |
| Chef de chantier | `chef1` / `chef2` | `Chef1#2026` / `Chef2#2026` |
| Ingénieur | `ing1` | `Ing1#2026` |
| Administrateur | `admin` | `Admin#2026` |

### Passage en production
- Lancer avec `SCREENLEAD_DEMO=0` : aucune donnée de démo, seul un compte `admin` est créé
  (mot de passe via `SCREENLEAD_ADMIN_PASSWORD`, sinon affiché une fois dans la console).
- Les comptes créés ont un mot de passe temporaire : le changement est exigé (bandeau) à la première connexion.
- Placer l'application derrière HTTPS (reverse proxy) et sauvegarder régulièrement `screenlead.db`.
- Autres variables : `SCREENLEAD_DB` (chemin de la base), `SCREENLEAD_DEVISE` (devise affichée).

## Droits d'accès par profil

Principe du moindre privilège : chacun ne voit **que les chantiers auxquels il est affecté**
(l'administrateur voit tout).

| Fonction | Maître d'ouvrage | Chef de chantier | Ingénieur | Admin |
|---|:-:|:-:|:-:|:-:|
| Tableau de bord, analyses | ✅ | ✅ (sans finances) | ✅ | ✅ |
| Budgets, CPI, coût final estimé | ✅ | ❌ | ✅ | ✅ |
| Journal de chantier : lecture | ✅ | ✅ | ✅ | ✅ |
| Journal de chantier : saisie | ❌ | ✅ | ❌ | ❌ |
| Journal de chantier : validation | ❌ | ❌ | ✅ | ❌ |
| Dépenses : consulter tout | ✅ | ❌ (ses saisies seulement) | ✅ | ✅ |
| Dépenses : saisir | ❌ | ✅ | ✅ | ❌ |
| Essais : lecture | ✅ | ✅ | ✅ | ✅ |
| Essais : saisie | ❌ | ❌ | ✅ | ❌ |
| Incidents : déclarer | ❌ | ✅ | ✅ | ❌ |
| Incidents : clôturer | ❌ | ❌ | ✅ | ❌ |
| Créer un chantier / affecter l'équipe | ✅ | ❌ | ❌ | ✅ |
| Gérer les comptes | ❌ | ❌ | ❌ | ✅ |
| Export CSV | ✅ | ❌ | ✅ | ✅ |
| Messagerie | ✅ | ✅ | ✅ | ✅ |

Justification métier : le chef de chantier produit la donnée de terrain ; l'ingénieur contrôle
(essais, validation, clôture) pour garantir l'indépendance du contrôle ; le maître d'ouvrage pilote
sans modifier les données techniques ; l'administrateur gère les accès sans intervenir sur le contenu.

## Messagerie
- **Messages directs** (réception, envoi, réponse) entre personnes partageant au moins un chantier
  (l'administrateur est joignable par tous), avec référence facultative à un chantier.
- **Canaux de chantier** : fil de discussion partagé par toute l'équipe affectée.
- Compteur de messages non lus dans le menu.

## Analyses fournies
- **Avancement** : courbe réel vs prévu, SPI, retard estimé, vitesse moyenne vs vitesse requise.
- **Coûts** : dépenses cumulées, répartition par catégorie, CPI, coût final estimé (valeur acquise).
- **Qualité** : taux de conformité par type d'essai, résultats vs seuils. Un essai non conforme ouvre
  automatiquement une non-conformité.
- **Sécurité** : incidents par type et gravité, taux de fréquence des accidents.
- **Ressources & météo** : effectifs, heures, rendement, impact de la météo sur l'avancement.
- **Comparaison** multi-chantiers et **alertes** automatiques (retard, dérive des coûts, non-conformités, incidents graves).

## Structure des données (SQLite)
`users`, `chantiers`, `affectations`, `journal`, `couts`, `essais`, `incidents`, `messages`.
Pour une utilisation multi-sites à plus grande échelle, la couche d'accès (`q`, `run`, `one`, `scalar`)
est isolée et peut être remplacée par PostgreSQL.
