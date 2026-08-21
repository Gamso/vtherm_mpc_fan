# Estimation des effective slopes par vitesse — analyse et conclusions

> **⚠️ STATUT : le modèle grey-box décrit ici a été SUPPRIMÉ le 26/07/2026.**
> L'analyse de la cause racine (biais de sélection en boucle fermée) reste
> valable et instructive, mais la solution proposée — modèle enveloppe
> `k_env`/`u_fan`, gate de faisabilité, fit contraint par débit d'air — a été
> retirée du code : `k_env` s'est révélé instable en production (τ variant de
> 20 h à ∞ voire négatif) parce qu'il absorbe la modulation du compresseur au
> lieu de mesurer l'enveloppe du bâtiment. Voir
> [`audit_apprentissage.md`](audit_apprentissage.md) pour les mesures et la
> décision. Document conservé comme trace de l'investigation.

_Investigation du 09/07/2026, sur la trace de production de 899 h
(`smart_fan_controller_data_01KNA9CY.csv`, mode cool) fusionnée avec un export
de température extérieure (`sensor.gardanne_temperature`). Reproductible via
`scripts/analyze_effective_slope_tint.py`._

## Le problème

Seule l'effective slope de `superhigh` était apprise de façon fiable ;
`silent`/`low`/`med` ressortaient **surestimées** (elles ont dû être forcées à la
main sur le système en production).

## Cause racine — biais de sélection en boucle fermée, pas un manque de données

Il y a largement assez de temps passé sur chaque vitesse en cool (silent 100 h,
low 97 h, med 149 h, high 187 h, superhigh 496 h). Ce qui manque, c'est la
**diversité d'excitation** : le contrôleur passe en `superhigh` dès que l'erreur
grandit, donc les vitesses faibles ne sont observées que dans une **plage
d'erreur étroite près de l'équilibre** (silent err[-0.4..1.0], low err[0.0..2.8]
mais quasi tout groupé près de 0, med err[-0.2..1.4], contre superhigh
err[-1.2..**4.6**]). Près de l'équilibre, la vitesse de variation est ~0 et
dominée par la charge externe, pas par le ventilateur. La régression par vitesse
`pente = a + b·erreur` cale alors un gain `b` bruité sur une plage d'erreur
quasi dégénérée, puis l'**extrapole** jusqu'à l'écart de référence (1 °C) — d'où
la surestimation qu'il a fallu corriger à la main. **Aucun estimateur ne peut
retrouver la capacité d'un ventilateur à 3 °C d'écart si ce ventilateur ne tourne
jamais à 3 °C d'écart.**

## Modèle grey-box (1R1C) avec le capteur extérieur

Avec `T_ext` disponible, le modèle du premier ordre devient directement
identifiable :

```
dT/dt = k_env·(T_ext − T) + u_fan
```

une conductance d'enveloppe partagée `k_env` (= 1/τ) plus une puissance propre
par vitesse `u_fan`. Le terme d'enveloppe est une physique indépendante du
ventilateur : il est calé par les données riches de `superhigh` ; chaque vitesse
faible n'a plus qu'à identifier **un seul scalaire**, ce que même 30–56
échantillons suffisent à faire. Fit (4211 échantillons) :

| vitesse | `u_fan` (°C/h) | écart extérieur max encore tenu à plat |
|---|---|---|
| silent | +0.22 (réchauffe) | — |
| low | −0.15 | +5.3 °C |
| med | −0.29 | +10.2 °C |
| high | −0.80 | +27.9 °C |
| superhigh | −1.21 | +42.0 °C |

`R²=0.19`, `RMSE≈0.85 ≈` écart-type du signal (0.95) : le résidu est surtout du
**bruit de quantification** sur la dérivée à 2 min d'un capteur à 0.2 °C — un
plancher de bruit, pas un défaut de modèle (la validation par bins colle bien
pour les vitesses bien échantillonnées).

## `u_fan` n'est **pas** constant (la mise en garde sur la linéarité)

`u_fan` est une moyenne conditionnelle — une moyenne sur les conditions où
chaque vitesse a été observée. En regroupant le refroidissement livré (`−dT/dt`)
par vitesse × erreur de confort (proxy de la demande compresseur), on voit qu'il
est fortement dépendant de la demande et **saturant** :

| vitesse | err≈0 | [+0.2,+0.6) | [+0.6,+1.2) | [+1.2,+2.0) |
|---|---|---|---|---|
| low | +0.01 | **−0.07** | — | — |
| med | +0.11 | **−0.12** | — | — |
| high | +0.10 | +0.78 | +1.12 | +1.38 |
| superhigh | +0.14 | +0.47 | +1.11 | +1.63 (puis 1.33 : **sature**) |

Deux conclusions robustes, à demande égale (bande d'erreur appariée) :

1. **`low` et `med` ne refroidissent pas cette pièce sous charge réelle** — à
   demande égale ils la **réchauffent** (−0.07, −0.12). Il y a un seuil net entre
   `med` et `high`. C'est réel, ce n'est pas qu'un biais de sélection.
2. **`high` ≈ `superhigh`** à demande égale (+1.12 vs +1.11, +1.38 vs +1.63) :
   cette pièce n'a en pratique que ~2 vraies vitesses de refroidissement,
   `low`/`med` faisant office de simple recirculation.

Le vrai plafond de capacité des vitesses faibles est **fondamentalement
inobservable** (elles ne tournent jamais à forte demande) — il ne faut donc pas
le fitter, mais on connaît le fait opérationnel important : elles ne tiennent
pas.

## Valeurs concrètes à utiliser pour la simulation

La simulation actuelle utilise **une** effective slope par vitesse, évaluée à
`REFERENCE_SLOPE_ERROR = 1 °C` (positive = refroidit vers la cible). La lecture
la plus directe est la table à demande appariée ci-dessus dans la bande
`[+0.6,+1.2)` (qui correspond justement à ~1 °C d'écart), complétée par le
modèle d'enveloppe pour les vitesses faibles jamais vues sous charge :

| vitesse | effective slope à ~1 °C (°C/h) | commentaire |
|---|---|---|
| silent | **≈ 0** (voire négatif) | aucun refroidissement utile ; recirculation |
| low | **≈ 0.0** | ne tient pas sous charge — surtout **pas** une valeur positive franche |
| med | **≈ 0.1** | marginal (refroidit un peu près de l'équilibre, perd du terrain sous charge) |
| high | **≈ 1.0–1.1** | première vitesse réellement efficace |
| superhigh | **≈ 1.1** | à peine plus forte que `high` |

C'est cohérent avec ce que le système avait appris pour `superhigh` (~1.05, sa
seule estimation fiable) et ça **valide ton forçage manuel** : les valeurs
auto-apprises pour low/med étaient trop hautes parce qu'elles extrapolaient un
gain `b` bruité depuis des échantillons tous pris près de l'équilibre.

**Attention — ces valeurs dépendent de la température extérieure.** Le refroidis-
sement net effectif vers la cible = `−(k_env·(T_ext−T) + u_fan)`. À la consigne :

| vitesse | doux (T_ext−T = +4) | tiède (+8) | chaud (+12) |
|---|---|---|---|
| low | +0.04 | −0.08 | −0.19 |
| med | +0.18 | +0.06 | −0.05 |
| high | +0.69 | +0.57 | +0.46 |
| superhigh | +1.10 | +0.98 | +0.86 |

C'est précisément pourquoi une constante par vitesse est un pis-aller et pourquoi
le modèle d'enveloppe existe : `low` **tient** quand il fait doux (jusqu'à +5 °C
d'écart extérieur) mais **pas** quand il fait chaud. Si tu forces des constantes,
prends les valeurs de la colonne « tiède » (silent ≈ 0, low ≈ 0, med ≈ 0.1,
high ≈ 0.6, superhigh ≈ 1.0) ; sinon, laisse le modèle d'enveloppe (le champ
capteur extérieur) piloter la faisabilité par condition.

## La limite du 1R1C : masse des murs, et le cas 2R2C

La critique est **juste et importante** : le 1R1C regroupe l'air et la masse
des murs (béton) dans une seule capacité. Juste après une hausse de ventilation,
l'air réagit en quelques minutes mais les murs, non — mesurer la pente de l'air à
cet instant **surestime** la capacité soutenable, et quand la PAC module, les
murs re-cèdent (ou re-pompent) des calories et la température rebondit. Un modèle
**2R2C** — un nœud `C_air` (faible inertie, réagit au ventilateur) + un nœud
`C_murs` (grosse inertie, stockage lent) — sépare ces deux échelles de temps et
prédit ce rebond. Pour un MPC air/air, c'est effectivement le modèle de
référence, et le conseil est sain.

Deux nuances pour **ce** projet, à garder honnêtes :

1. **On atténue déjà la confusion air/mur à l'apprentissage.** Le code ne collecte
   les échantillons de pente qu'en phase `ESTABLISHED` (après `dead_time` ×
   facteur), en écartant `DEAD_TIME`/`TRANSIENT`. C'est un proxy empirique de
   « attendre que le système air+mur se stabilise » — donc on n'apprend pas la
   pente sur le transitoire rapide de l'air. La table à demande appariée ci-dessus
   confirme d'ailleurs que le refroidissement soutenu (bande établie) est plus
   faible que le pic transitoire.

2. **Identifiabilité et architecture.** Un 2R2C a deux états pour **une seule**
   mesure (l'air) : sans capteur de température de paroi/rayonnante, le pôle lent
   n'est identifiable que faiblement, à partir d'événements de réponse libre de
   plusieurs heures (PAC coupée) — rares et pollués par le solaire. De plus notre
   contrôleur n'est pas un planificateur en boucle ouverte sur plusieurs heures :
   c'est une boucle de rétroaction à horizon glissant qui **re-mesure la
   température de l'air toutes les 2 min** et re-décide. L'effet des murs y est
   donc largement **observé** (il apparaît dans la pente mesurée et dans le
   `disturbance_bias`) plutôt que devant être prédit à l'avance. Le gain du 2R2C
   est maximal pour la prédiction long-horizon / l'anticipation du rebond, moins
   pour une rétroaction courte.

**Verdict.** Le 2R2C est le bon modèle sur le fond et une direction future
légitime — mais son plein bénéfice suppose soit **un capteur de paroi/rayonnant**
(ex. capteur IR sur un mur, ou une température radiante moyenne), soit un
engagement à identifier le pôle lent sur des réponses libres. Pour le contrôleur
à horizon court actuel, la garde de phase + l'hystérésis + le couplage au
dead-time atténuent déjà l'oscillation de rebond que la critique pointe. À
réévaluer si, une fois le modèle d'enveloppe déployé, une oscillation liée à
l'inertie des murs persiste sur l'appareil : le prochain pas serait alors 2R2C
avec un capteur radiant.

## Ce qu'on en fait

Le contrôleur n'ingérait aucune température extérieure. Deux changements, tous
deux pilotés par `T_ext` et rétro-compatibles (dormants sans capteur extérieur) :

1. **Nettoyage d'enveloppe (apprentissage).** Le fit à effets fixes sépare la
   puissance propre `u_fan` de la charge ambiante, donnant une estimation par
   vitesse enfin non contaminée — attaque directement la surestimation de
   `silent`/`low`/`med`.
2. **Garde de faisabilité (contrôle).** À partir de `k_env` et `u_fan`, on prédit
   l'équilibre de chaque vitesse ; une vitesse incapable de tenir la consigne à la
   température extérieure actuelle est écartée des candidats à la baisse (escalade
   seulement). Ça remplace la garde brute par rang par un test capacité-vs-charge
   physiquement fondé : on autorise `low`/`med` quand il fait doux (ils tiennent)
   et on les bloque quand il fait chaud.

Validé hors-ligne sur cette trace avant tout redéploiement. Le capteur Gardanne
est une station météo de ville (pas la façade du logement) : il porte la
tendance mais rate le solaire direct par les fenêtres ; le résiduel reste dans
l'EMA `disturbance_bias`.
