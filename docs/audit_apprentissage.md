# Audit de l'apprentissage et de l'approche « débit d'air »

_Audit du 19/07/2026, sur une trace de production en mode cool
(`smart_fan_controller_data_01KNA9CY.csv`, 2009 échantillons ESTABLISHED)
fusionnée avec l'export extérieur `sensor.gardanne_temperature` (`outdoor.csv`).
Décomposition de variance reproductible via
`scripts/audit_variance_decomposition.py` ; fits enveloppe/débit via
`scripts/analyze_effective_slope_tint.py`. Fait suite à
[`effective_slope_analysis.md`](effective_slope_analysis.md)._

## Question posée

L'apprentissage par `effective_slope` semble insatisfaisant : certaines vitesses
(silent/low/med) ne sont quasiment jamais utilisées. L'approche consistant à
renseigner le débit d'air nominal par vitesse (`u_fan = a + b·débit`) est-elle
la bonne réponse ?

## Résultat central — décomposition de la variance de dT/dt

Séquence de modèles OLS emboîtés sur la même trace, R² de chacun :

| Modèle | Contenu | R² |
|---|---|---|
| M0 | constante seule | 0.000 |
| M1 | `k_env·(Text−T)` (conductance enveloppe) | 0.024 |
| M2 | M1 + puissance propre par vitesse (**fit libre actuel**) | 0.029 |
| M3 | `k_env + a + b·débit` (**fit contraint par débit**) | 0.026 |
| **M4** | **M2 + erreur de confort (terme de demande)** | **0.224** |
| M5 | M2 + erreur×vitesse (pente de demande par vitesse) | singulier¹ |

¹ singulier car `med` (4 éch.) et `high` (8 éch.) ne peuvent pas supporter leur
propre pente de demande — encore la privation de données.

**Lecture.** Toute la machinerie enveloppe — conductance + puissance ventilateur,
que le fit soit libre (M2) ou contraint par débit (M3) — explique **~3 %** de la
variation de dT/dt. Le terme de **demande** (distance à la consigne, M4) en
explique **~22 %, soit ~8× plus**. Le débit d'air n'ajoute quasiment rien au-dessus
de la conductance seule (M3 vs M1 : +0.002).

Autrement dit : le modèle enveloppe (et donc son raffinement par débit) est
**mal spécifié** — il a troqué le terme dominant (demande) contre un terme faible
(charge extérieure). On peaufine un modèle à 3 %.

## Verdict sur l'approche « débit d'air »

**Intention correcte, mais traite le mauvais problème, et de façon marginale.**

Ce qu'elle apporte réellement :

- Un classement **monotone** (low < med < high < superhigh) là où le fit libre
  donnait l'aberration `high` > `superhigh` (bruit de sous-échantillonnage). Vrai
  gain de robustesse du classement.
- Optionnelle, rétro-compatible, bien gatée. Aucun risque tant que le débit n'est
  pas renseigné.

Ses limites :

1. **Pouvoir explicatif quasi nul** (M3 vs M1 : +0.002). Le classement monotone
   vient surtout de la **structure imposée** (ordonner par débit), pas d'une
   découverte des données.
2. **Le modèle enveloppe sous-jacent est mal spécifié** (voir M4) : il omet le
   terme dominant.
3. **L'hypothèse `a + b·débit` est linéaire**, alors que le rendement d'un
   échangeur sature à haut débit et peut s'effondrer à bas débit (relation
   probablement concave). L'extrapolation linéaire est la moins fiable exactement
   là où elle compte le plus : la vitesse la plus faible, celle qui alimente la
   **gate de faisabilité**.

### Le risque de sécurité concret

Pour `low` :

| source | `u_fan` (°C/h) |
|---|---|
| fit contraint par débit | −0.304 |
| fit libre (19 éch. réels, bruités) | −0.166 |

À 36 °C extérieur (écart ≈ 12 °C à la consigne 24 °C), avec `k_env ≈ 0.025` :

- débit : `0.025·12 − 0.304 = −0.004` → jugé **capable de tenir** (à la limite).
- libre : `0.025·12 − 0.166 = +0.134` → jugé **incapable** (réchauffe).

Les deux modèles **divergent sur la décision de sécurité**. Si la vérité est
proche de −0.166, le modèle débit **surestime low et peut réintroduire la pièce
bloquée au-dessus de la consigne** (l'incident d'origine à 24.4 °C).

## La vraie cause racine

La gêne « certaines vitesses ne sont jamais utilisées » n'est probablement **pas
un défaut d'apprentissage**. C'est :

1. **Un problème d'identifiabilité, pas de forme de modèle.** Une vitesse jamais
   excitée sous charge ne peut pas voir son paramètre identifié — c'est un
   théorème, pas un manque d'astuce. Régression contrainte, prior débit, retrait
   du filtre de stagnation : rien de tout ça ne *fabrique* l'information
   manquante, ça la *devine*.
2. **Peut-être un comportement correct.** superhigh = 98 % des cycles. À Gardanne
   l'été (32–38 °C dehors), il est plausible que low/med **ne puissent
   physiquement pas** tenir la charge — le contrôleur a alors raison de ne pas les
   utiliser. Le tableau par bande de demande le montrait : `low` délivre
   ~−0.03 °C/h même près de l'équilibre (ne refroidit quasiment pas).

Le piège : le prior débit rend le contrôleur **plus enclin** à essayer les
vitesses faibles (estimations optimistes). Si elles sont réellement inadéquates,
c'est **pire**, pas mieux.

## Nuance importante : le gap model n'est pas le coupable

Le modèle historique `effective_slope(error) = a + b·error` **contient le terme
dominant** (la demande, M4). Ce qui déçoit dans `effective_slope`, ce n'est pas
sa *forme*, c'est le **biais de sélection sur les vitesses faibles** — un problème
de **données**, pas de modèle. C'est pourquoi le débit (qui change la forme) ne le
règle pas.

## Ce qui serait réellement satisfaisant : l'exploration active

Le seul moyen de savoir si `low` tient à 34 °C, c'est de **le mesurer** : quand
c'est sûr (pièce déjà à/sous la consigne, extérieur doux), descendre délibérément
d'un cran, observer 2–3 dead-times, enregistrer. Ça génère la donnée qu'aucune
régression ne synthétise — le compromis explore/exploit classique en
identification de système. Concrètement : une sonde d'exploration à faible taux,
gatée par la même sécurité que la gate de faisabilité, alimentant le modèle
enveloppe avec de vrais échantillons de vitesses faibles.

## Architecture : trois modèles, deux régimes

Il y a trois modèles parallèles de la même physique :

- gap model `a + b·error` — porte la **demande** (loin de la consigne, recovery) ;
- enveloppe libre `k_env·gap + u_fan` — porte l'**extérieur** (près de la consigne,
  holding / faisabilité) ;
- enveloppe débit `k_env·gap + (a + b·débit)` — variante contrainte du précédent.

Ce n'est **pas incohérent** : le gap model et l'enveloppe sont en fait spécialisés
à deux régimes différents (la demande domine loin de la consigne, l'extérieur
domine à la consigne où la demande ≈ 0). Mais **ce découpage régime-dépendant
n'est écrit nulle part** — il mériterait d'être documenté explicitement, sinon une
évolution future risque de casser l'un en croyant améliorer l'autre.

Le reste du pipeline (gates dead-time, disturbance bias, monotone enforcement,
fenêtre glissante avec rétention minimale par profil) est sain et couvert par les
tests.

## Recommandations initiales (26/07 : dépassées, voir la suite)

1. **Garder** le fit débit (inoffensif, améliore le classement) mais **ne pas
   compter dessus pour la sécurité** : faire consommer à la gate de faisabilité la
   valeur **la plus conservatrice** entre fit libre et fit débit pour les vitesses
   faibles, plutôt que l'extrapolation optimiste. Correctif à faible risque.
2. **Investir dans l'exploration active** — seul levier qui attaque la vraie cause
   (l'identifiabilité).
3. **Documenter** le découpage à deux régimes (gap = demande, enveloppe =
   extérieur/faisabilité) directement dans le code.

---

# Suite du 26/07/2026 — `k_env` est instable : le modèle enveloppe est supprimé

_Nouvelle trace : 10 695 lignes du 11/07 au 26/07 (4810 en cool), avec la colonne
`outdoor_temp` désormais présente dans le CSV — plus besoin d'export externe._

## `k_env` dérive d'un facteur 10 et change de signe

Fit à effets fixes en fenêtre glissante 72 h, reproduisant le gate d'échantillonnage
de l'intégration :

| Fenêtre | `k_env` | τ | `u_superhigh` |
|---|---|---|---|
| 13 juil | 0.0506 | 20 h | −0.572 |
| 17 juil | 0.0318 | 31 h | −0.579 |
| 18 juil | 0.0047 | **214 h** | −0.535 |
| 20 juil | **−0.0160** | **négatif** | −0.356 |
| 24 juil | **−0.1071** | **négatif** | −0.535 |
| 25 juil | **−0.0808** | **négatif** | −0.805 |

Le bâtiment n'a pas changé. `u_superhigh` reste, lui, raisonnablement stable
(−0.53 à −0.80) : **c'est `k_env` seul qui part en vrille**, et comme la gate
consomme `k_env·gap + u_fan` avec un gap de ~+10 °C, c'est lui qui injecte toute
l'instabilité dans la décision de sécurité (composite observé : +1.97 à −1.61).

## Cause : `k_env` mesure le compresseur, pas l'enveloppe

`k_env` est identifié à partir de la variation de `(T_ext − T)`. Or le compresseur
**module en réponse aux mêmes conditions** : quand il fait chaud, le gap est grand
*et* le compresseur pousse plus fort. La régression ne peut pas séparer les deux et
attribue le comportement du compresseur au terme « enveloppe ». `k_env` ne mesure
donc pas l'isolation du logement mais « comment le compresseur réagit à la
température extérieure » — ni constant, ni ce qu'on veut. **Contamination
inévitable en boucle fermée sans observer la modulation du compresseur.**

## Le fit contraint par débit dégénère aussi

Depuis le ~15/07 le contrôleur n'utilise plus que `high` et `superhigh` : le fit
trace une droite à travers **2 débits groupés** (468, 666) puis **extrapole** vers
300 (low) et 216 (silent). Résultat sur les mêmes fenêtres :

| Fenêtre | coefficient `b` (par m³/h) | `u_low` extrapolé |
|---|---|---|
| 13 juil | −0.000065 | −0.546 |
| 16 juil | −0.021925 | **+7.493** 🚨 |
| 17 juil | **+0.006592** (signe inversé) | −2.992 |
| 24 juil | −0.006369 | **+1.796** |

Le signe de `b` s'inverse d'une fenêtre à l'autre (`b > 0` = « plus de débit
réchauffe », absurde) et `u_low` atteint +7.5 °C/h. Le garde-fou « ≥ 2 débits
distincts » est trop faible : il autorise exactement ce cas. Ceci **corrige** la
validation initiale de ce rapport, faite sur une fenêtre où 4 débits distincts
étaient présents.

## Contexte : le système est saturé 40 % du temps

**1935 lignes sur 4810 (40 %)** portent « Saturated: max fan, still short of
target », pour seulement **52 changements de ventilateur en 4810 cycles**. Et
l'apprentissage des vitesses faibles est structurellement mort : en cool
ESTABLISHED, `low` a 19 échantillons (1 passe le filtre anti-stagnation), `med` 4
(3 passent), `silent` 0 — le seuil de 10 par profil n'est jamais atteint.

Le contrôleur n'ignore pas injustement les vitesses faibles : il est collé en haut
**parce que la pièce en a besoin**. L'absence d'apprentissage est un symptôme, pas
la maladie.

## Décision : suppression complète du modèle enveloppe

Un modèle non fiable qu'il faut borner pour l'empêcher de nuire ne gagne pas sa
complexité. Supprimés : `k_env`/`u_fan` et tout le fit à effets fixes, le fit
contraint par débit et sa saisie en config, la gate de faisabilité grey-box,
`USE_ENVELOPE_PROJECTION` et son entité switch, les entités τ et Envelope Power,
et les 5 colonnes CSV associées.

**Conservé** : le capteur extérieur optionnel et sa colonne `outdoor_temp` dans le
CSV, en **télémétrie pure** (aucune décision de contrôle ne la lit) — c'est ce qui
a permis cet audit, et ça préserve la possibilité de réanalyser plus tard.

La gate de faisabilité revient à la garde d'origine par rang brut, adossée au
filet de sécurité indépendant qu'est l'escalade sur croissance de l'erreur
(`DEAD_TIME_ESCALATION_GROWTH`) — c'est ce dernier qui protège réellement contre
l'incident d'origine (pièce bloquée au-dessus de la consigne).

## Ce qui reste pertinent

- **`effective_slope` (modèle gap `a + b·erreur`) est le bon modèle** : il porte le
  terme dominant (la demande, 22 % de variance). Sa faiblesse est un problème de
  **données** sur les vitesses faibles, pas de forme.
- **`deadband`** reste structurel : il définit la zone morte du coût, la zone de
  maintien d'équilibre et les marges de commutation. 54 % du temps est passé à
  moins de 0.2 °C de la consigne — c'est le régime dominant.
- **`min_interval`** reste utile, mais son rôle réel est « plancher **et** plafond
  (×3) de l'intervalle adaptatif déduit du temps mort appris » (observé : 18→30 min
  effectifs, actif sur 13 % des lignes). Les libellés de configuration ont été
  précisés en ce sens.
- **Prochain levier de fond** : l'exploration active, et surtout **la modulation du
  compresseur** si la PAC l'expose (fréquence ou puissance instantanée). C'est le
  signal qui expliquerait la variance que `k_env` tentait d'absorber en vain — bien
  plus utile que la température extérieure pour un MPC de vitesse de ventilateur.
