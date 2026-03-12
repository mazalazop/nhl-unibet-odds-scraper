# NHL Unibet Odds Scraper

Repo dédié au scraping des marchés joueurs NHL chez Unibet, avec une architecture simple, robuste et maintenable.

## Objectif

Ce repo sert à extraire et normaliser des cotes joueurs NHL utiles pour un projet plus global de modélisation / value betting.

Les marchés ciblés actuellement sont :

- **GOALS / BUTEUR**
- **POINTS**

Le principe retenu est simple :

- **un seul workflow officiel par marché**
- **un seul chemin officiel par marché**
- pipeline :
  - batch
  - acceptance
  - normalize
  - upload artifacts

---

## Marchés métier retenus

### GOALS / BUTEUR

Règle métier :

- garder uniquement la cote **`Buteur`**
- ne pas garder :
  - `2 buts ou plus`
  - `points`
  - `double chance`
  - autres marchés voisins ou parasites

### POINTS

Règle métier :

- garder uniquement la cote **`1 ou plus`**
- un joueur = une seule cote `1 ou plus`
- ne pas garder :
  - `2 ou plus`
  - `3 ou plus`
  - autres lignes parasites

---

## Workflows officiels

Les seuls workflows officiels à utiliser sont :

- `.github/workflows/unibet-event-goals-batch.yml`
- `.github/workflows/unibet-event-points-batch.yml`

Les anciens workflows parser / normalize non batch sont considérés comme **legacy** et ne doivent plus être utilisés comme point d’entrée principal.

---

## Scripts officiels

### GOALS

- `scrapers/unibet_event_goals_batch_runner.py`
- `scrapers/unibet_event_goals_parser_v1.py`
- `scrapers/unibet_event_goals_acceptance_report.py`
- `scrapers/unibet_event_goals_normalize_accepted.py`

### POINTS

- `scrapers/unibet_event_points_batch_runner.py`
- `scrapers/unibet_event_points_parser_v3.py`
- `scrapers/unibet_event_points_acceptance_report.py`
- `scrapers/unibet_event_points_normalize_accepted.py`

---

## Architecture retenue

Pour chaque marché, le pipeline officiel est :

1. **batch runner**
2. **acceptance report**
3. **normalize accepted**
4. **upload artifacts**

Philosophie :

- simplicité
- robustesse
- maintenabilité
- cohérence entre marchés
- pas de workflows automatiques parasites si possible

---

## Runner GitHub Actions

Le repo utilise un runner self-hosted installé sur le Mac de l’utilisateur.

### Labels attendus

```yaml
runs-on: [self-hosted, macOS, X64, morgan-runner]
