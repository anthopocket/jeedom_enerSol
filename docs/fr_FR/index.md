# enerSol — gestion de l'énergie solaire

enerSol prévoit la production solaire et la consommation, simule la batterie heure par heure sur 48 h, planifie la journée et conseille pour les jours suivants. Un contrôleur temps réel (toutes les 10 s) sert la batterie d'abord, puis répartit le surplus. Il apprend sur vos compteurs (cycles des machines, chauffage, chauffe-eau, VMI), et vous pouvez lui donner vos propres valeurs de chauffage.


## Passer de energIA à enerSol

enerSol est le nouveau nom d'energIA (l'IA a été retirée). À l'installation, enerSol reprend tout seul ce qu'avait energIA :
- les **équipements et commandes**, avec les mêmes numéros : scénarios, designs et historiques restent valables ;
- les **réglages** du plugin ;
- les **données apprises** : cycles des machines, chauffage, chauffe-eau.

Marche à suivre :
1. Dans energIA, arrêtez le démon et désactivez la gestion automatique. **Ne supprimez pas** energIA tout de suite.
2. Installez enerSol, activez-le, installez les dépendances, puis démarrez le démon.
3. Le log enerSol doit dire « Reprise de energIA terminée ».
4. Vous pouvez alors supprimer le plugin energIA.

## Installation

1. Copier le dossier `enerSol` dans `/var/www/html/plugins/` (ou installer le zip), activer le plugin.
2. **Dépendances** : bouton « Relancer » dans la page du plugin. Crée un venv Python dans `resources/venv` et installe `aiohttp`. Fonctionne sur **Debian 11 et Debian 12** : sur Debian 12 le Python du système (3.11) est utilisé ; sur Debian 11 (Python 3.9, trop ancien) un Python 3.12 autonome est téléchargé dans `resources/python` (environ 110 Mo), sans toucher au Python du système.
3. Configuration du plugin : mesures (production, batterie, réseau, consommation, température extérieure), batterie, confort. La localisation utilisée pour la météo est celle de Jeedom (Réglages → Système → Configuration → Général).
4. Page du plugin → **Ajouter** un équipement par appareil (batterie, pompe, PAC, lave-linge, sèche-linge, chauffe-eau, voiture, VMI) et renseigner ses commandes.
5. Aucun autre plugin ne doit piloter les mêmes appareils.

## Fonctionnement

| Couche | Fréquence | Rôle |
|---|---|---|
| Temps réel | 10 s | Lecture groupée (1 requête), décisions par appareil, actions groupées |
| Planificateur | 15 min | Prévisions J→J+3, simulation SOC, réserve de nuit, créneaux machines, préchauffage |
| Météo | 1 h | Open-Meteo (irradiance sur le plan des panneaux + température) |
| Historique | chaque nuit | Apprentissage conso = f(heure, température) sur 45 jours |

### Prévision PV
Solcast pour aujourd'hui (et le total de demain), Open-Meteo pour J+1 à J+3. Le modèle apprend un coefficient **par heure et par saison** en comparant production réelle et irradiance : il intègre les ombrages d'hiver. Les heures où l'onduleur bride (batterie pleine, pas d'export) ne sont pas utilisées.

### Hiver : consommer puis recharger
- **Réserve de nuit** : SOC nécessaire pour passer la nuit avec la consommation de base prévue (dépend de la température). Les consommations non essentielles sur batterie s'arrêtent sous ce seuil.
- **Stockage de chaleur** : si les consignes de la PAC sont réglables, surchauffe en temps réel sur le soleil perdu, légère baisse le soir si la batterie risque de se vider.

### Garde-fous
- SOC jamais sous le minimum configuré ; réserve de nuit ≥ SOC minimum.
- Décalage de consigne borné (± valeur configurée), jamais sous la température de confort minimale.
- Appareil jamais commandé si « Laisser enerSol piloter cet appareil » est décoché, ou s'il est désactivé.

## Points à vérifier chez vous

- **Consignes PAC** : les commandes « Régler consigne » (slider) sont nécessaires pour la surchauffe solaire et le préchauffage.
- **Température extérieure** : indispensable aux modèles de consommation et de chauffage.
- **Pas de recharge depuis le réseau** : enerSol ne recharge jamais la batterie sur le réseau.


## Programmes appris (lave-linge, sèche-linge)

Dans l'équipement du lave-linge ou du sèche-linge, la section **Programmes appris** liste chaque programme et chaque température enregistrés. Pour chacun, elle affiche :
- le nombre de cycles ;
- l'énergie moyenne et la durée ;
- le pic de puissance ;
- la courbe de puissance, une barre par tranche de 5 min (survolez une barre pour voir sa valeur).

En dessous, les 10 derniers cycles sont listés. Le bouton 🗑 oublie un programme mal appris (par exemple un cycle interrompu) ; il sera réappris au prochain cycle.

### Mode « Ne rien faire »

Ce mode existe pour le lave-linge et le sèche-linge. Pour l'activer, cochez **Ne rien faire (observer et enregistrer seulement)** dans l'équipement. Vous pouvez aussi utiliser les actions « Ne rien faire (conseil seulement) » et « Laisser enerSol lancer » de l'équipement.

Dans ce mode :
- enerSol n'envoie jamais l'ordre de démarrage, même si la machine attend un démarrage à distance (hOn) ;
- vous lancez la machine vous-même, sur place ou à distance ;
- les cycles sont quand même enregistrés : programme, température, énergie, durée et courbe ;
- l'avis solaire, c'est-à-dire le meilleur moment pour lancer, reste affiché ;

Le bouton ✏️ renomme un programme appris, par exemple un cycle enregistré « programme inconnu » avant que le nom hOn soit lu. Si le nouveau nom existe déjà pour la même température, les deux sont fusionnés : moyenne des cycles et des courbes.

## Pompe piscine (filtration)

- **Durée** : celle préconisée par iopool, lue le matin et gardée pour la journée.
- **Uniquement sur le soleil** : la pompe démarre quand il y a au moins le « surplus mini » de production en trop. Ce surplus peut être de l'injection réseau, ou de la charge batterie si la batterie sera de toute façon pleine aujourd'hui.
- **Arrêt** : quand la durée est atteinte, ou quand la maison tire plus de 100 W sur la batterie ou le réseau pendant 3 minutes (passage nuageux prolongé). Elle repart si le soleil revient.
- **Pas de marche forcée** : si la durée n'est pas atteinte au coucher du soleil, tant pis. Pas de filtration la nuit ni en mode hiver iopool.
- **Démarrage en deux vitesses** : V1 pendant 20 s pour l'amorçage, puis V2.

## Chauffe-eau

Deux types, à choisir dans l'équipement.

### Thermodynamique (pompe à chaleur + résistance d'appoint)
Le ballon se gère seul. enerSol ne fait que **décaler sa chauffe sur le soleil** avec les commandes Auto / Arrêt :
- **Auto** toute la journée, et le soir jusqu'à l'heure d'arrêt réglée (21:00 par défaut) ;
- **Arrêt** la nuit, jusqu'au retour du soleil le matin ;
- garde-fous, car il n'y a pas de mesure de l'eau chaude :
  - jamais plus de 12 h à l'arrêt (réglable) ;
  - **pas d'arrêt** la nuit si le solaire prévu le lendemain est faible (moins de 5 kWh, réglable) ;
- commandes « Chauffe-eau ON » = Auto forcé, « OFF » = Arrêt forcé, « Auto » = retour au fonctionnement normal.

### Électrique classique (délestage solaire)
- **« Délestage solaire actif » décoché** : enerSol observe seulement (consommation), il n'envoie aucune commande.
- **Coché** : le jour, dès que le surplus atteint le « surplus mini », enerSol allume le chauffe-eau. Il l'éteint si le surplus disparaît ou le soir.
- **Priorité batterie** : si la batterie ne se remplira pas aujourd'hui et que demain est déficitaire, le délestage est bloqué.
- enerSol n'arrête **que la chauffe qu'il a lancée lui-même**. La chauffe des heures creuses ou du thermostat du ballon n'est jamais coupée, sauf commande « Chauffe-eau OFF » explicite.
- Si le ballon est déjà chaud (pas de consommation après ON), enerSol attend 15 min avant de réessayer.
- La mesure de puissance doit être juste (positive quand il chauffe) : c'est elle qui indique si le ballon chauffe.

## Priorité à la batterie et programmes des machines

- Le solaire sert **d'abord à remplir la batterie**. Un lancement de machine n'est conseillé que si la batterie sera quand même pleine le soir, ou avec du surplus qui serait perdu.
- **Sans programme sélectionné**, aucun conseil de lancement : enerSol ne devine pas le programme.

### Commandes des équipements Lave-linge et Sèche-linge
Chaque machine a ses propres commandes, sur **son équipement** :
- **Conseil enerSol** : « Je la lancerai à 10h20 (…) » si enerSol a le droit de la lancer et que le démarrage à distance est prêt, sinon « Conseil : lancez-la à 10h20 (…) ». Autres textes possibles : « Aucun programme sélectionné… » ou « en cours, fin dans 45 min ».
- **Programme sélectionné** : programme et température choisis le matin, et ce qu'enerSol en sait (appris, ou estimation).
- **Pilotage** : qui lance la machine (enerSol ou vous).
- **En cours**, **Puissance**, **Dernier cycle**, **Ne rien faire**.
- Actions : **Ne rien faire (conseil seulement)**, **Laisser enerSol lancer**, **Lancer maintenant**.

## Correction « nuages »

À chaque calcul (toutes les 15 min), si les 2 dernières heures ont produit moins (ou plus) que prévu, les prochaines heures du jour sont corrigées d'autant. L'effet s'estompe sur 4 h. Les heures où l'onduleur bride (batterie pleine) ne sont pas utilisées.

### Lecture du programme (hOn)
« Programme sélectionné » affiche le programme **dès qu'il est choisi** sur la machine. enerSol le lit tout de suite : c'est lui qui sert au conseil et au calcul avec la courbe apprise. Après le départ, hOn rafraîchit la valeur toutes les minutes (cron). À partir d'1 min, la valeur lue confirme le programme réellement en cours, et c'est sous ce programme que le cycle est enregistré.

## Surchauffe solaire en temps réel (PAC maison)

Toutes les 10 s, si **la batterie est pleine et que du surplus est perdu** (export, ou production supérieure à la consommation avec la batterie pleine), enerSol **monte la consigne de la PAC de 0,5 °C toutes les 5 min**. La maison stocke alors ce soleil en chaleur (ou en fraîcheur en climatisation).
- Elle redescend de 0,5 °C si la maison tire sur la batterie ou le réseau pendant 5 min.
- Elle revient aux consignes habituelles hors solaire.
- Elle ne dépasse jamais la température intérieure max (23 °C par défaut) ni le décalage max (1,5 °C par défaut).
- Le log l'indique : « PAC maison → surchauffe solaire +1.0 °C (surplus perdu ~900 W, batterie 100 %) ». La commande « PAC maison — décision » affiche l'état en cours.

Il faut que la PAC maison soit configurée avec ses commandes de consigne (« Régler consigne » par pièce, consigne et température par pièce, mode), avec le pilotage « consigne » ou « les deux ».

## Machines : lancement sur le soleil perdu, en temps réel

Quand un programme est sélectionné et que **du surplus est perdu depuis 2 min** (batterie pleine), enerSol vérifie que ce surplus couvre au moins 70 % du **cycle appris** de ce programme. Il compte pour cela le surplus du moment et celui encore prévu aujourd'hui. Si c'est le cas :
- si enerSol a le droit de piloter et que le démarrage à distance est prêt : **il lance la machine tout de suite**, même avant l'heure prévue ou si le plan disait « demain » ;
- sinon (mode « ne rien faire », démarrage à distance non activé) : **il vous envoie une notification** « Soleil perdu en ce moment : lancez le lave-linge… », une fois toutes les 3 h au plus.

Le chauffage (surchauffe PAC) passe avant les machines : une machine part quand la maison a pris ce qu'elle pouvait stocker. Si la machine ne démarre pas après l'ordre, enerSol attend 30 min avant de réessayer.

## Voiture (borne de recharge)

Créez un appareil **« Voiture (borne de recharge) »** et renseignez au minimum « Démarrer / Arrêter la charge » et la puissance de charge. L'intensité réglable et « voiture branchée » sont recommandées.

- **Sur le surplus uniquement** : la charge démarre après 2 min de surplus suffisant (6 A mini, soit ~1,4 kW en monophasé). L'intensité suit le surplus toutes les 30 s, et la charge s'arrête après 3 min sans assez de surplus, ou au coucher du soleil. La batterie maison passe d'abord : sa charge ne va à la voiture que si elle sera pleine aujourd'hui.
- **Sans réglage d'intensité** : démarrage si le surplus dépasse la puissance de charge réglée, arrêt si la maison tire plus de 300 W pendant 3 min.
- **Protection de la batterie maison** : si la voiture charge **hors surplus** (lancée à la main, charge forcée, la nuit), enerSol **coupe la décharge de la batterie maison**. La voiture et la maison sont alors alimentées par le réseau, et la batterie reste pour la nuit. La décharge est rétablie dès la fin de la charge, au démarrage et à l'arrêt du démon. Renseignez « Batterie : interdire / autoriser la décharge » (actions) ou un slider « décharge max (W) ».
- Commandes du maître : « Voiture ON » (charge forcée), « OFF », « Auto ».
- Ordre de priorité du surplus : batterie, machines, voiture, surchauffe PAC.

## L'intelligence : arbitrage et apprentissage

### Arbitre du surplus (toutes les 10 s)
L'objectif, dans cet ordre :
1. **recharger la batterie** ;
2. **gérer la température** de la maison : en hiver, le chauffage passe avant le reste (surchauffe PAC, puis résistance VMI) ;
3. **s'il en reste, conseiller ou lancer** : machines, voiture, pompe, chauffe-eau.

Tout est réajusté toute la journée selon le soleil réel, la température, les machines et les imprévus. Le but n'est pas de se passer du réseau, mais d'en acheter le moins possible sans sacrifier le confort.
- **Un seul démarrage à la fois.** Après chaque démarrage, 3 min d'attente laissent les mesures intégrer la nouvelle consommation. Ainsi, tous les appareils ne partent pas sur le même surplus.
- **Un appareil prioritaire qui attend garde sa place** : un moins prioritaire ne peut pas lui prendre son surplus.
- **Un appareil qui ne démarre pas après l'ordre** (borne, pompe, machine) n'est pas relancé avant 30 min.

### Journal
- Chaque jour, enerSol enregistre ce qui s'est **réellement** passé dans `data/journal.json` (30 jours) : production, consommation, **achat réseau** (dont en journée), **surplus perdu estimé**, heures de batterie pleine, voiture sur ou hors surplus, surchauffe, et les **décisions prises** avec leur heure.

### Modèle thermique de la maison (appris)
Toutes les 10 min en chauffage, enerSol note la température intérieure, la température extérieure et l'énergie de la PAC. Il en apprend :
- les **kWh pour monter la maison de 1 °C** ;
- la **constante de temps**, c'est-à-dire combien de temps la maison garde sa chaleur ;
- les **heures que la maison tient sans chauffer**.

Le modèle est jugé fiable après ~2 jours de chauffage (fichier `data/thermal.json`). Il sert à :
- chiffrer ce que rapporte +0,5 / +1 / +1,5 °C stockés maintenant : kWh stockés, part qui reste à 19h, kWh de PAC évités le soir ;
- ne pas stocker trop tôt : si moins de 40 % de la chaleur resterait à 19h, la surchauffe se limite à +0,5 °C.

### Consommation selon le type de jour
Le modèle de consommation apprend en plus un facteur par heure pour la **semaine**, le **week-end** et l'**absence** (commande « présence » de la configuration). Les prévisions du soir et de la nuit en tiennent compte.

### Lave-linge → sèche-linge
À la fin d'un lavage, enerSol envoie une notification avec le conseil de séchage, calculé avec le programme de séchage habituel appris (énergie, durée) et le surplus du moment ou prévu :
- « lancez maintenant » ;
- ou « sélectionnez le programme, il sera lancé au bon moment » ;
- ou « pas assez de soleil aujourd'hui : étendez le linge ».

## VMI (ventilation par insufflation)

Créez un appareil **« VMI »**. Toutes les 10 s, enerSol pilote trois choses.

**1. La source d'air**, en pilotant les registres :
- **hiver** : la source gratuite la plus chaude. Les combles ou le capteur solaire s'ils sont au moins 2 °C au-dessus de l'intérieur, sinon l'air extérieur s'il est plus chaud que dedans, sinon le **recyclage** de l'air intérieur ;
- **été** : air extérieur, et **freecooling** quand l'extérieur est plus frais que l'intérieur (au-dessus de 24 °C dedans) ;
- au moins 10 min entre deux changements de registres.

**2. Le ventilateur (0-10 V)** :
- au minimum réglé pour le renouvellement d'air ;
- au maximum quand l'air pris est gratuitement plus chaud (ou plus frais en été) ;
- au moins 60 % quand la résistance chauffe.

**3. La résistance (0-10 V)** : elle est **modulée sur le seul surplus perdu**, c'est-à-dire batterie pleine (ou pleine aujourd'hui de toute façon) et production en trop.
- Elle passe en **dernier** dans l'ordre du surplus, car son rendement est de 1, contre environ 3 pour la PAC.
- Elle baisse immédiatement si la maison tire sur la batterie.
- Elle est coupée hors solaire, à la température intérieure max, ou si le soufflage dépasse 45 °C.

La résistance utilisée sur le surplus est notée dans le journal (`vmi_resistance_surplus_kwh`).

## Tuile « Ma journée »

Commande **« Ma journée »** du maître, affichée en tuile sur le dashboard (gabarit `enerSol::synthese`). Elle répond à trois questions :
- 🔋 **Est-ce qu'on tient la nuit ?** (batterie prévue au retour du soleil) ;
- 🔌 **Va-t-on acheter au réseau ?** (combien, à partir de quelle heure) ;
- 🏠 **Le confort est-il respecté ?** (température intérieure).

Dessous, **les décisions du jour**, en clair : faites ✅, en cours ▶️, prévues ⏳, imprévus ⚡. Elle est mise à jour toutes les 30 s.

## Imprévus (four, plaques…)

enerSol ne prévoit pas de réserve fixe : il **réagit quand ça arrive**. Un appel de plus de 1,5 kW au-dessus de la consommation habituelle du moment, qui ne vient pas d'un appareil piloté, et qui dure plus de 30 s, est traité comme un imprévu :
- enerSol coupe aussitôt la résistance VMI et la surchauffe de la PAC ;
- il met la voiture au minimum (ou l'arrête sans réglage d'intensité) ;
- il arrête la pompe si elle tire sur la batterie, et repousse le lancement des machines ;
- aucun nouveau démarrage n'a lieu pendant l'imprévu ;
- tout reprend 2 min après la fin de l'imprévu, et le plan est recalculé ;
- l'imprévu est noté dans le journal et affiché dans la tuile.

## Pic d'une machine : faire de la place sans toucher au confort

Quand une machine tourne, qu'elle ait été lancée par enerSol, forcée ou démarrée à la main, enerSol connaît sa **phase gourmande** grâce à la courbe apprise : par exemple la chauffe de l'eau du lave-linge, ~2 kW pendant 20 min.

Si, pendant cette phase, la maison **achète au réseau** ou **vide la batterie** :
- **le chauffage fait une courte pause** (consigne −1 °C, le temps du pic), seulement si le modèle thermique garantit que la maison reste au-dessus du confort minimum. Sinon, le chauffage est maintenu ;
- la résistance VMI, la surchauffe, la voiture et la pompe lâchent, et aucun autre appareil ne démarre ;
- tout reprend dès que le pic est passé.

La charge de la batterie baisse d'elle-même : le Victron sert d'abord la maison. Le pic est affiché dans la tuile (« Lave-linge chauffe l'eau : je fais de la place ») et noté dans le journal.

## Expérience : ce que les compteurs apprennent

enerSol mesure ce que rapporte **vraiment** 1 kWh de soleil donné à chaque appareil, en « kWh évités plus tard » (achetés au réseau ou pris à la batterie). Les résultats sont dans la commande **« Ce que j'ai appris (compteurs) »** de l'équipement principal.

- **Chauffe-eau** : enerSol suit son compteur chaque jour (au soleil, puis la nuit qui suit). Si chauffer plus au soleil fait vraiment moins consommer ensuite, le rendement est proche de 1. Il est plus bas si le ballon était de toute façon presque chaud. Le compteur donne aussi la consommation habituelle par jour, ce qu'il reste à chauffer aujourd'hui, et l'heure où le ballon est chaud (en Auto, il ne consomme plus). Il faut une semaine de mesures ; avant, la valeur par défaut est 0,8.
- **VMI** : le modèle thermique sépare les °C apportés par la PAC, par la résistance VMI et par l'air des combles. 1 kWh de résistance VMI évite X kWh de PAC, mesuré chez vous. Avant d'être appris, la valeur est 0,33, car la PAC est environ 3 fois plus efficace. Cet apprentissage demande la PAC maison configurée.
- **Chauffe-eau ou VMI ?** Quand le chauffe-eau passe sur sa résistance (~1,8 kW) en journée, il est mis en pause si trois conditions sont réunies :
  - la maison a besoin de chaleur ;
  - la VMI rapporte nettement plus (au moins 20 %) ;
  - le soleil qui reste aujourd'hui suffit encore pour l'eau.

  Dans ce cas, le soleil va à la VMI. La pause dure 45 min au plus, 2 h par jour au plus, jamais dans les 2 h et demie avant la fin du soleil, et jamais par temps couvert. L'eau chauffe ensuite au soleil.
- **PAC ou VMI ?** Si les mesures montrent qu'1 kWh de résistance VMI chauffe mieux la maison qu'1 kWh de PAC (PAC peu efficace par grand froid), la VMI passe devant la surchauffe PAC.

### Chauffage appris selon la température extérieure

Le modèle thermique apprend ses valeurs **par tranche de température dehors** : moins de 0 °C, 0 à 5, 5 à 10, 10 à 15 et plus de 15 °C. Une tranche est apprise après environ 10 h de mesures à cette température. En attendant, la moyenne générale sert.

- **T-One** : kWh électriques pour monter la maison de 1 °C. Il en faut plus quand il gèle, car la PAC rend moins.
- **Refroidissement** : °C que la maison perd par heure sans chauffer, selon le froid dehors.
- **VMI** : kWh de résistance pour +1 °C, selon le froid dehors (l'air neuf est plus froid) et selon la consigne 0-10 V, par palier : jusqu'à 40 %, 40 à 70 %, plus de 70 %.
- **Résistance VMI** : avec la commande « Puissance mesurée », enerSol apprend les watts réels consommés à chaque palier de 10 % du 0-10 V. Il règle alors la consigne pour consommer exactement le surplus.

Sans PAC configurée, enerSol apprend quand même le refroidissement et la VMI, avec la sonde intérieure de la VMI.

Tout est affiché dans **« Ce que j'ai appris (compteurs) »**. Ces valeurs servent à chiffrer la surchauffe, la tenue de la nuit et le chauffage des jours suivants.

## Stabilité : batterie d'abord, pas d'allers-retours

Ces règles ont été mises au point en simulant des journées complètes : soleil, nuages, temps gris, four, machine, voiture, VMI et chauffe-eau ensemble.

- **Batterie d'abord, vérifiée en temps réel.** Un appareil (voiture, résistance VMI, pompe) ne prend la charge de la batterie que si elle est **en avance**. Être en avance veut dire que le soleil restant aujourd'hui couvre ce qui lui manque, avec une marge. La marge vaut 1,3 fois le besoin, plus le matin car la prévision est moins sûre (jusqu'à 2 fois). Ce soleil restant est corrigé par ce que les compteurs mesurent réellement sur la dernière demi-heure (production moins consommation). Si le chauffage consomme plus que prévu, la batterie cesse aussitôt d'être en avance. Sinon, les appareils n'ont que ce que la batterie ne peut pas absorber.
- **Ordre du surplus** :
  1. surchauffe PAC ;
  2. machines ;
  3. voiture ;
  4. résistance VMI ;
  5. pompe ;
  6. chauffe-eau.

  Une machine ou la voiture évite environ 1 kWh acheté plus tard par kWh de soleil. La résistance VMI n'évite qu'environ 0,3 kWh de PAC. Si les compteurs montrent que la VMI chauffe mieux que la PAC, elle passe en tête.
- **En cascade** : la voiture prend d'abord, et la VMI n'a que ce que la voiture ne peut plus prendre. Les deux ne se disputent plus le même surplus.
- **Réglages lissés** :
  - résistance VMI : surplus moyenné sur environ 1 min, réglage au plus une fois par minute et seulement si l'écart dépasse 15 %, marche ou arrêt après 2 min stables, ventilateur à deux paliers ;
  - voiture : intensité changée d'au moins 2 A, et 10 min d'attente avant de relancer après un arrêt.

  Seule exception : si la maison tire sur la batterie ou le réseau, tout baisse immédiatement.
- **Pause pendant un pic de machine** : la décision est prise une fois au début du pic et gardée jusqu'à la fin. Elle s'arrête si la maison atteint le confort minimal.
- **Stockage de chaleur** : uniquement en temps réel, sur le soleil réellement perdu. Le planificateur ne monte plus la consigne d'après la prévision, et il garde seulement la légère baisse du soir.
- **Surveillance** : chaque appareil a un nombre d'ordres normal par heure (40 pour un réglage progressif, 8 pour une marche/arrêt, une consigne ou des registres). Au-delà, l'appareil est **calmé 30 min** : réglages 3 fois plus lents, aucun nouveau démarrage. C'est noté dans le journal et dans la tuile.

## Apprendre le chauffage sur des années d'historique

Si Jeedom historise depuis longtemps la **température extérieure**, la **puissance du chauffage** (W) et la **température du salon**, enerSol apprend le chauffage d'un coup, sans attendre des semaines de mesures. Dans la configuration du plugin, section **Apprentissage du chauffage (historique)** :

- **Puissance du chauffage** : la puissance instantanée de la T-One. Si le champ est vide, enerSol prend la puissance de la PAC maison.
- **Température intérieure de référence** : le salon. Si le champ est vide, enerSol prend celle de la PAC maison.
- **Historique à apprendre** : 1095 jours (3 ans) par défaut, 0 pour ne rien importer.

La température extérieure est celle des **Mesures**.

Au démarrage du démon, enerSol lit l'historique par tranches de 90 jours, pour ne pas charger Jeedom (environ une minute pour 3 ans). Il fait des moyennes horaires **pondérées par le temps** : une valeur vaut jusqu'à la suivante, et un trou de plus de 2 h compte comme une absence de mesure. Il ne garde que les heures de chauffe (d'octobre à avril, moins de 16 °C dehors) et la **nuit**, de 19 h à 7 h, car le soleil qui entre par les fenêtres le jour fausserait le calcul.

Il apprend ensuite, pour chaque tranche de température extérieure (moins de 0 °C, 0-5, 5-10, 10-15 °C) :
- les kWh de la T-One pour monter de 1 °C ;
- les °C que la maison perd par heure sans chauffer.

Le résultat s'affiche dans **« Ce que j'ai appris (compteurs) »**. Les mesures en direct complètent ensuite l'historique, avec les mêmes sondes. L'import est refait seulement si vous changez une de ces commandes.

## Chauffage : valeurs connues (saisie manuelle)

Dans la configuration du plugin, le tableau **« Chauffage : valeurs connues (manuel) »** permet de donner ce que vous savez de votre maison, pour 6 températures extérieures : -10, -5, 0, 5, 10 et 15 °C.
- **kWh de chauffage pour gagner 1 °C** : l'énergie électrique de la PAC pour monter la maison de 1 °C.
- **Descente sans chauffer (°C/h)** : combien la maison perd par heure sans chauffage, quand elle est à 20 °C.

Règles :
- une case remplie est **utilisée telle quelle**, avant tout ce qui est appris ;
- une case vide prend la **valeur apprise** (historique et compteurs) ;
- entre deux températures renseignées, enerSol **interpole** ; au-delà des bornes, il garde la valeur la plus proche.

Le bouton **« Reprendre les valeurs apprises »** remplit le tableau avec ce que le modèle a appris. Vous corrigez ce qui vous semble faux, puis **Sauvegarder**. Les valeurs utilisées s'affichent dans « Ce que j'ai appris (compteurs) ».

L'apprentissage continue en arrière-plan, et il servira notamment pour la VMI.

### Chauffage zoné (étage seul / toute la maison)

Si votre PAC ne chauffe que l'étage à faible puissance et toute la maison à pleine puissance, renseignez dans « Apprentissage du chauffage (historique) » :
- **puissance où TOUTE la maison chauffe** (1000 W par défaut) ;
- **puissance où seul l'étage chauffe** (500 W par défaut, 0 s'il n'y a pas de zones).

Entre les deux, la part est répartie en proportion. Les kWh pour +1 °C sont appris **seulement sur l'énergie qui chauffe toute la maison**. L'effet de l'étage seul sur la sonde de référence (salon) est appris à part et affiché. Pour l'historique, enerSol compte le temps passé à chaque puissance dans l'heure, ce qui est plus juste qu'une simple moyenne horaire.
