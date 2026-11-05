<?php
/* This file is part of enerSol — Gestion de l'énergie solaire pour Jeedom */

require_once __DIR__ . '/../../../../core/php/core.inc.php';

class enerSol extends eqLogic {

    const DEFAULT_SOCKET_PORT = 55790;

    /* ==================================================================
     *  SCHÉMAS DE CONFIGURATION
     *  - type : cmd_info | cmd_action | number | text | select | checkbox | seasons
     *  - d    : clé côté démon (section de l'appareil)
     * ================================================================== */

    public static function globalSchema(): array {
        $fc = [];
        for ($h = 6; $h <= 21; $h++) {
            $k = 'cmd_forecast_h' . str_pad($h, 2, '0', STR_PAD_LEFT);
            $fc[] = ['key' => $k, 'label' => sprintf('Prévision Solcast %02dh (Wh)', $h), 'type' => 'cmd_info'];
        }
        return [
            'Mesures' => [
                ['key' => 'cmd_pv_power',          'label' => 'Puissance PV (W)',               'type' => 'cmd_info'],
                ['key' => 'cmd_battery_soc',       'label' => 'SOC batterie (%)',               'type' => 'cmd_info'],
                ['key' => 'cmd_grid_power',        'label' => 'Puissance réseau (W, <0 = injection)', 'type' => 'cmd_info'],
                ['key' => 'cmd_load_power',        'label' => 'Consommation maison (W)',        'type' => 'cmd_info'],
                ['key' => 'cmd_text',              'label' => 'Température extérieure (°C)',    'type' => 'cmd_info'],
                ['key' => 'cmd_season',            'label' => 'Saison (spring/summer/autumn/winter)', 'type' => 'cmd_info'],
                ['key' => 'cmd_presence',          'label' => 'Présence (binaire)',             'type' => 'cmd_info'],
            ],
            self::HEAT_TABLE_SECTION => self::heatTableFields(),
            'Apprentissage du chauffage (historique)' => [
                ['key' => 'cmd_heat_power', 'label' => 'Puissance du chauffage (W, historisée — sinon celle de la PAC maison)', 'type' => 'cmd_info'],
                ['key' => 'cmd_tint_ref',   'label' => 'Température intérieure de référence, ex. salon (historisée — sinon celle de la PAC maison)', 'type' => 'cmd_info'],
                ['key' => 'heat_history_days', 'label' => 'Historique à apprendre (jours, 0 = aucun)', 'type' => 'number', 'default' => 1095],
                ['key' => 'heat_whole_w', 'label' => 'Chauffage zoné : puissance où TOUTE la maison chauffe (W)', 'type' => 'number', 'default' => 1000],
                ['key' => 'heat_floor_w', 'label' => 'Chauffage zoné : puissance où seul l\'étage chauffe (W, 0 = pas de zones)', 'type' => 'number', 'default' => 500],
            ],
            'Prévisions Solcast' => array_merge([
                ['key' => 'cmd_forecast_tomorrow', 'label' => 'Prévision PV demain (Wh)', 'type' => 'cmd_info'],
                ['key' => 'solar_threshold_wh',    'label' => 'Seuil plage solaire (Wh/h)', 'type' => 'number', 'default' => 100],
            ], $fc),
            'Batterie & seuils' => [
                ['key' => 'battery_capacity_wh',     'label' => 'Capacité utile batterie (Wh)', 'type' => 'number', 'default' => 10000],
                ['key' => 'soc_critical',            'label' => 'SOC minimum (%)',              'type' => 'number', 'default' => 20],
                ['key' => 'appliance_min_solar_pct', 'label' => 'Part solaire minimale pour lancer une machine (%)', 'type' => 'number', 'default' => 80],
                ['key' => 'hysteresis_off',          'label' => 'Hystérésis arrêt (W)',         'type' => 'number', 'default' => -100],
            ],
            'Confort et notifications' => [
                ['key' => 'cmd_notify', 'label' => 'Commande de notification (message, ex : Pushover)', 'type' => 'cmd_action'],
                ['key' => 'comfort_min_temp', 'label' => 'Température intérieure minimale hiver (°C)', 'type' => 'number', 'default' => 19],
                ['key' => 'comfort_max_offset', 'label' => 'Écart max de consigne (°C)', 'type' => 'number', 'default' => 1.5],
                ['key' => 'surplus_boost_max', 'label' => 'Surchauffe solaire : décalage max (°C, 0 = jamais)', 'type' => 'number', 'default' => 1.5],
                ['key' => 'comfort_max_temp', 'label' => 'Surchauffe solaire : température intérieure max (°C)', 'type' => 'number', 'default' => 23],
                ['key' => 'comfort_min_cool', 'label' => 'Surrefroidissement solaire : température intérieure min (°C)', 'type' => 'number', 'default' => 23],
                ['key' => 'surplus_boost_soc', 'label' => 'Surchauffe solaire : batterie considérée pleine à (%)', 'type' => 'number', 'default' => 97],
            ],
            'Démon' => [
                ['key' => 'socketport', 'label' => 'Port socket interne', 'type' => 'number', 'default' => self::DEFAULT_SOCKET_PORT],
                ['key' => 'cycle',      'label' => 'Cycle temps réel (s)', 'type' => 'number', 'default' => 10],
            ],
        ];
    }

    /** Valeurs de chauffage connues, saisies à la main (de -10 à 15 °C dehors). Vides = valeurs apprises. */
    const HEAT_TABLE_SECTION = 'Chauffage : valeurs connues (manuel)';
    const HEAT_TABLE_TEMPS = [-10, -5, 0, 5, 10, 15];

    public static function heatTableFields(): array {
        $out = [];
        foreach (self::HEAT_TABLE_TEMPS as $t) {
            $tag = $t < 0 ? 'm' . abs($t) : (string)$t;
            $out[] = ['key' => 'heat_kwh_' . $tag,  'label' => "Dehors $t °C : kWh pour +1 °C", 'type' => 'heat', 'temp' => $t, 'col' => 'kwh'];
            $out[] = ['key' => 'heat_drop_' . $tag, 'label' => "Dehors $t °C : descente sans chauffer (°C/h)", 'type' => 'heat', 'temp' => $t, 'col' => 'drop'];
        }
        return $out;
    }

    /** Valeurs apprises par le modèle (data/thermal.json), pour pré-remplir le tableau manuel. */
    public static function learnedHeatTable(): array {
        $out = ['values' => [], 'missing' => [], 'r2' => null, 'fiable' => false, 'r2_mini' => 0.35, 'heures' => 0];
        $file = self::dataDir() . '/thermal_table.json';
        if (!is_file($file)) return $out;
        $d = json_decode((string)file_get_contents($file), true);
        if (!is_array($d) || !isset($d['rows'])) return $out;
        foreach (['r2', 'fiable', 'r2_mini', 'heures'] as $k) {
            if (isset($d[$k])) $out[$k] = $d[$k];
        }
        $seen = [];
        foreach ($d['rows'] as $row) {
            if (!isset($row['t'])) continue;
            $seen[] = intval($row['t']);
            $tag = $row['t'] < 0 ? 'm' . abs($row['t']) : (string)$row['t'];
            if (isset($row['kwh'])) $out['values']['heat_kwh_' . $tag] = $row['kwh'];
            if (isset($row['drop'])) $out['values']['heat_drop_' . $tag] = $row['drop'];
        }
        foreach (self::HEAT_TABLE_TEMPS as $t) {
            if (!in_array($t, $seen, true)) $out['missing'][] = $t;
        }
        return $out;
    }

    /** Champs communs (actif / saisons / pilotage). L'ordre de priorité est fixe : batterie, température, puis le reste. */
    private static function planningFields(string $p, string $seasons): array {
        return [
            ['key' => $p . '_enabled',       'd' => 'enabled',       'label' => 'Actif', 'type' => 'checkbox', 'default' => 1],
            ['key' => $p . '_seasons',       'd' => 'seasons',       'label' => 'Saisons actives', 'type' => 'seasons', 'default' => $seasons],
            ['key' => $p . '_ai_control',    'd' => 'ai_control',    'label' => 'Laisser enerSol piloter cet appareil', 'type' => 'checkbox', 'default' => 1],
        ];
    }

    public static function deviceSchema(): array {
        $appliance = function (string $p, string $label, int $prio, int $minSurplus, int $energy, string $optLabel) {
            return [
                'label'   => $label,
                'section' => $p,
                'fields'  => array_merge([
                    ['key' => $p . '_observe_only',    'd' => 'observe_only',    'label' => 'Ne rien faire (observer et enregistrer seulement)', 'type' => 'checkbox', 'default' => 0],
                    ['key' => $p . '_cmd_start',       'd' => 'cmd_start',       'label' => 'Démarrer', 'type' => 'cmd_action'],
                    ['key' => $p . '_cmd_pause',       'd' => 'cmd_pause',       'label' => 'Pause', 'type' => 'cmd_action'],
                    ['key' => $p . '_cmd_resume',      'd' => 'cmd_resume',      'label' => 'Reprise', 'type' => 'cmd_action'],
                    ['key' => $p . '_cmd_remaining',   'd' => 'cmd_remaining',   'label' => 'Temps restant (mn)', 'type' => 'cmd_info'],
                    ['key' => $p . '_cmd_power',       'd' => 'cmd_power',       'label' => 'Puissance (W)', 'type' => 'cmd_info'],
                    ['key' => $p . '_cmd_hon_waiting', 'd' => 'cmd_hon_waiting', 'label' => 'Connecté / prêt (hOn)', 'type' => 'cmd_info'],
                    ['key' => $p . '_cmd_program',     'd' => 'cmd_program',     'label' => 'Programme sélectionné', 'type' => 'cmd_info'],
                    ['key' => $p . '_cmd_program_state', 'd' => 'cmd_program_state', 'label' => 'Programme en cours (optionnel — vide : « Programme sélectionné » est lu dès la sélection, puis confirmé 1 min après le départ, cron hOn)', 'type' => 'cmd_info'],
                    ['key' => $p . '_cmd_temperature', 'd' => 'cmd_temperature', 'label' => $optLabel, 'type' => 'cmd_info'],
                    ['key' => $p . '_cmd_energy',      'd' => 'cmd_energy',      'label' => 'Compteur d\'énergie (kWh, optionnel)', 'type' => 'cmd_info'],
                    ['key' => $p . '_min_surplus',     'd' => 'min_surplus',     'label' => 'Surplus mini (W)', 'type' => 'number', 'default' => $minSurplus],
                    ['key' => $p . '_cycle_energy_wh', 'd' => 'cycle_energy_wh', 'label' => 'Énergie d\'un cycle si non apprise (Wh)', 'type' => 'number', 'default' => $energy],
                ], self::planningFields($p, 'spring,summer,autumn,winter')),
            ];
        };

        return [
            'battery' => [
                'label'   => 'Batterie (pilotage charge)',
                'section' => 'battery',
                'fields'  => [
                    ['key' => 'battery_enabled',              'd' => 'enabled',              'label' => 'Pilotage actif', 'type' => 'checkbox', 'default' => 1],
                    ['key' => 'battery_cmd_charge_slider',    'd' => 'cmd_charge_slider',    'label' => 'Limite de charge (slider W)', 'type' => 'cmd_action'],
                    ['key' => 'battery_cmd_force_charge_on',  'd' => 'cmd_force_charge_on',  'label' => 'Charge forcée ON', 'type' => 'cmd_action'],
                    ['key' => 'battery_cmd_force_charge_off', 'd' => 'cmd_force_charge_off', 'label' => 'Charge forcée OFF', 'type' => 'cmd_action'],
                    ['key' => 'battery_charge_max_w',         'd' => 'charge_max_w',         'label' => 'Charge max (W)', 'type' => 'number', 'default' => 3500],
                    ['key' => 'battery_force_charge_soc',     'd' => 'force_charge_soc',     'label' => 'SOC déclenchant la charge forcée de fin de journée (%)', 'type' => 'number', 'default' => 95],
                    ['key' => 'battery_ai_control',           'd' => 'ai_control',           'label' => 'Laisser enerSol piloter la batterie', 'type' => 'checkbox', 'default' => 1],
                ],
            ],
            'pump_pool' => [
                'label'   => 'Pompe piscine',
                'section' => 'pump',
                'fields'  => array_merge([
                    ['key' => 'pump_cmd_on_v1',            'd' => 'cmd_on_v1',            'label' => 'ON vitesse 1', 'type' => 'cmd_action'],
                    ['key' => 'pump_cmd_on_v2',            'd' => 'cmd_on_v2',            'label' => 'ON vitesse 2', 'type' => 'cmd_action'],
                    ['key' => 'pump_cmd_off_v1',           'd' => 'cmd_off_v1',           'label' => 'OFF vitesse 1', 'type' => 'cmd_action'],
                    ['key' => 'pump_cmd_off_v2',           'd' => 'cmd_off_v2',           'label' => 'OFF vitesse 2', 'type' => 'cmd_action'],
                    ['key' => 'pump_cmd_state_v1',         'd' => 'cmd_state_v1',         'label' => 'État vitesse 1', 'type' => 'cmd_info'],
                    ['key' => 'pump_cmd_state_v2',         'd' => 'cmd_state_v2',         'label' => 'État vitesse 2', 'type' => 'cmd_info'],
                    ['key' => 'pump_cmd_power',            'd' => 'cmd_power',            'label' => 'Puissance (W)', 'type' => 'cmd_info'],
                    ['key' => 'pump_cmd_runtime_mn',       'd' => 'cmd_runtime_mn',       'label' => 'Temps de marche du jour (mn)', 'type' => 'cmd_info'],
                    ['key' => 'pump_cmd_iopool_mode',      'd' => 'cmd_iopool_mode',      'label' => 'iopool : mode', 'type' => 'cmd_info'],
                    ['key' => 'pump_cmd_iopool_temps_min', 'd' => 'cmd_iopool_temps_min', 'label' => 'iopool : durée de filtration (h)', 'type' => 'cmd_info'],
                    ['key' => 'pump_cmd_iopool_temp_eau',  'd' => 'cmd_iopool_temp_eau',  'label' => 'iopool : température eau', 'type' => 'cmd_info'],
                    ['key' => 'pump_power_v1',             'd' => 'power_v1',             'label' => 'Puissance V1 (W)', 'type' => 'number', 'default' => 500],
                    ['key' => 'pump_power_v2',             'd' => 'power_v2',             'label' => 'Puissance V2 (W)', 'type' => 'number', 'default' => 350],
                    ['key' => 'pump_min_surplus',          'd' => 'min_surplus',          'label' => 'Surplus mini (W)', 'type' => 'number', 'default' => 400],
                ], self::planningFields('pump_pool', 'spring,summer,autumn')),
            ],
            'pac_pool' => [
                'label'   => 'PAC piscine',
                'section' => 'pac_pool',
                'fields'  => array_merge([
                    ['key' => 'pac_pool_cmd_on',           'd' => 'cmd_on',           'label' => 'ON', 'type' => 'cmd_action'],
                    ['key' => 'pac_pool_cmd_off',          'd' => 'cmd_off',          'label' => 'OFF', 'type' => 'cmd_action'],
                    ['key' => 'pac_pool_cmd_state',        'd' => 'cmd_state',        'label' => 'État', 'type' => 'cmd_info'],
                    ['key' => 'pac_pool_cmd_power',        'd' => 'cmd_power',        'label' => 'Puissance (W)', 'type' => 'cmd_info'],
                    ['key' => 'pac_pool_cmd_temp_cible',   'd' => 'cmd_temp_cible',   'label' => 'Température cible (info)', 'type' => 'cmd_info'],
                    ['key' => 'pac_pool_temp_cible_local', 'd' => 'temp_cible_local', 'label' => 'Température cible par défaut (°C)', 'type' => 'number', 'default' => 28],
                    ['key' => 'pac_pool_nominal_w',        'd' => 'nominal_w',        'label' => 'Puissance nominale (W)', 'type' => 'number', 'default' => 2000],
                    ['key' => 'pac_pool_min_surplus',      'd' => 'min_surplus',      'label' => 'Surplus mini (W)', 'type' => 'number', 'default' => 800],
                ], self::planningFields('pac_pool', 'spring,summer')),
            ],
            'pac_home' => [
                'label'   => 'PAC maison',
                'section' => 'pac_home',
                'fields'  => array_merge([
                    ['key' => 'pac_home_control', 'd' => 'control', 'label' => 'Mode de pilotage', 'type' => 'select', 'default' => 'onoff',
                     'options' => ['onoff' => 'ON/OFF selon l\'énergie', 'consigne' => 'Ajustement des consignes seulement', 'both' => 'ON/OFF + consignes']],
                    ['key' => 'pac_home_cmd_on',    'd' => 'cmd_on',    'label' => 'ON', 'type' => 'cmd_action'],
                    ['key' => 'pac_home_cmd_off',   'd' => 'cmd_off',   'label' => 'OFF', 'type' => 'cmd_action'],
                    ['key' => 'pac_home_cmd_state', 'd' => 'cmd_state', 'label' => 'État', 'type' => 'cmd_info'],
                    ['key' => 'pac_home_cmd_power', 'd' => 'cmd_power', 'label' => 'Puissance (W)', 'type' => 'cmd_info'],
                    ['key' => 'pac_home_cmd_mode',  'd' => 'cmd_mode',  'label' => 'Mode (4 = chauffe, 2 = clim, 5 = arrêt)', 'type' => 'cmd_info'],
                    ['key' => 'pac_home_cmd_text',  'd' => 'cmd_text',  'label' => 'Température extérieure PAC', 'type' => 'cmd_info'],
                    ['key' => 'pac_home_cmd_consigne_salon', 'd' => 'cmd_consigne_salon', 'label' => 'Consigne salon (info)', 'type' => 'cmd_info'],
                    ['key' => 'pac_home_cmd_consigne_ch1',   'd' => 'cmd_consigne_ch1',   'label' => 'Consigne chambre 1 (info)', 'type' => 'cmd_info'],
                    ['key' => 'pac_home_cmd_consigne_ch2',   'd' => 'cmd_consigne_ch2',   'label' => 'Consigne chambre 2 (info)', 'type' => 'cmd_info'],
                    ['key' => 'pac_home_cmd_consigne_ch3',   'd' => 'cmd_consigne_ch3',   'label' => 'Consigne chambre 3 (info)', 'type' => 'cmd_info'],
                    ['key' => 'pac_home_cmd_set_consigne_salon', 'd' => 'cmd_set_consigne_salon', 'label' => 'Régler consigne salon (slider)', 'type' => 'cmd_action'],
                    ['key' => 'pac_home_cmd_set_consigne_ch1',   'd' => 'cmd_set_consigne_ch1',   'label' => 'Régler consigne chambre 1 (slider)', 'type' => 'cmd_action'],
                    ['key' => 'pac_home_cmd_set_consigne_ch2',   'd' => 'cmd_set_consigne_ch2',   'label' => 'Régler consigne chambre 2 (slider)', 'type' => 'cmd_action'],
                    ['key' => 'pac_home_cmd_set_consigne_ch3',   'd' => 'cmd_set_consigne_ch3',   'label' => 'Régler consigne chambre 3 (slider)', 'type' => 'cmd_action'],
                    ['key' => 'pac_home_cmd_tint_salon', 'd' => 'cmd_tint_salon', 'label' => 'T° salon', 'type' => 'cmd_info'],
                    ['key' => 'pac_home_cmd_tint_ch1',   'd' => 'cmd_tint_ch1',   'label' => 'T° chambre 1', 'type' => 'cmd_info'],
                    ['key' => 'pac_home_cmd_tint_ch2',   'd' => 'cmd_tint_ch2',   'label' => 'T° chambre 2', 'type' => 'cmd_info'],
                    ['key' => 'pac_home_cmd_tint_ch3',   'd' => 'cmd_tint_ch3',   'label' => 'T° chambre 3', 'type' => 'cmd_info'],
                    ['key' => 'pac_home_nominal_w',  'd' => 'nominal_w',  'label' => 'Puissance moyenne si inconnue (W)', 'type' => 'number', 'default' => 1000],
                    ['key' => 'pac_home_min_surplus','d' => 'min_surplus','label' => 'Surplus mini (W)', 'type' => 'number', 'default' => 1000],
                ], self::planningFields('pac_home', 'spring,summer,autumn,winter')),
            ],
            'washer'       => $appliance('washer', 'Lave-linge', 4, 1500, 1200, 'Température de lavage'),
            'dryer'        => $appliance('dryer', 'Sèche-linge', 5, 1500, 1800, 'Niveau de séchage / température'),
            'vmi' => [
                'label'   => 'VMI (ventilation par insufflation)',
                'section' => 'vmi',
                'fields'  => array_merge([
                    ['key' => 'vmi_cmd_fan',        'd' => 'cmd_fan',        'label' => 'Ventilateur (slider %, 0-10 V)', 'type' => 'cmd_action'],
                    ['key' => 'vmi_fan_min_pct',    'd' => 'fan_min_pct',    'label' => 'Ventilateur mini — renouvellement d\'air (%)', 'type' => 'number', 'default' => 30],
                    ['key' => 'vmi_fan_max_pct',    'd' => 'fan_max_pct',    'label' => 'Ventilateur maxi (%)', 'type' => 'number', 'default' => 100],
                    ['key' => 'vmi_cmd_heater',     'd' => 'cmd_heater',     'label' => 'Résistance (slider %, 0-10 V)', 'type' => 'cmd_action'],
                    ['key' => 'vmi_heater_max_w',   'd' => 'heater_max_w',   'label' => 'Puissance de la résistance à 100 % (W)', 'type' => 'number', 'default' => 2000],
                    ['key' => 'vmi_cmd_power',      'd' => 'cmd_power',      'label' => 'Puissance mesurée (W, optionnel)', 'type' => 'cmd_info'],
                    ['key' => 'vmi_cmd_air_attic',  'd' => 'cmd_air_attic',  'label' => 'Registres : air des combles / capteur solaire (action)', 'type' => 'cmd_action'],
                    ['key' => 'vmi_cmd_air_ext',    'd' => 'cmd_air_ext',    'label' => 'Registres : air extérieur (action)', 'type' => 'cmd_action'],
                    ['key' => 'vmi_cmd_air_recycle','d' => 'cmd_air_recycle','label' => 'Registres : recyclage air intérieur (action)', 'type' => 'cmd_action'],
                    ['key' => 'vmi_cmd_t_int',      'd' => 'cmd_t_int',      'label' => 'Température intérieure (reprise)', 'type' => 'cmd_info'],
                    ['key' => 'vmi_cmd_t_ext',      'd' => 'cmd_t_ext',      'label' => 'Température extérieure (sinon celle de la config)', 'type' => 'cmd_info'],
                    ['key' => 'vmi_cmd_t_attic',    'd' => 'cmd_t_attic',    'label' => 'Température combles / capteur solaire', 'type' => 'cmd_info'],
                    ['key' => 'vmi_cmd_t_blow',     'd' => 'cmd_t_blow',     'label' => 'Température de soufflage', 'type' => 'cmd_info'],
                    ['key' => 'vmi_t_target',       'd' => 't_target',       'label' => 'Température de confort hiver (°C)', 'type' => 'number', 'default' => 20],
                    ['key' => 'vmi_t_cool',         'd' => 't_cool',         'label' => 'Freecooling au-dessus de (°C intérieur)', 'type' => 'number', 'default' => 24],
                    ['key' => 'vmi_t_blow_max',     'd' => 't_blow_max',     'label' => 'Soufflage maxi (°C)', 'type' => 'number', 'default' => 45],
                    ['key' => 'vmi_source_min_s',   'd' => 'source_min_s',   'label' => 'Délai mini entre deux changements de registres (s)', 'type' => 'number', 'default' => 600],
                ], self::planningFields('vmi', 'spring,summer,autumn,winter')),
            ],
            'ev' => [
                'label'   => 'Voiture (borne de recharge)',
                'section' => 'ev',
                'fields'  => array_merge([
                    ['key' => 'ev_mode',         'd' => 'ev_mode',        'label' => 'Recharge', 'type' => 'select', 'default' => 'surplus',
                        'options' => ['surplus' => 'Sur le surplus solaire uniquement', 'observe' => 'Ne rien faire (observer)']],
                    ['key' => 'ev_cmd_on',       'd' => 'cmd_on',         'label' => 'Démarrer la charge', 'type' => 'cmd_action'],
                    ['key' => 'ev_cmd_off',      'd' => 'cmd_off',        'label' => 'Arrêter la charge', 'type' => 'cmd_action'],
                    ['key' => 'ev_cmd_current',  'd' => 'cmd_current',    'label' => 'Intensité de charge (slider A, optionnel)', 'type' => 'cmd_action'],
                    ['key' => 'ev_cmd_power',    'd' => 'cmd_power',      'label' => 'Puissance de charge (W)', 'type' => 'cmd_info'],
                    ['key' => 'ev_cmd_charging', 'd' => 'cmd_charging',   'label' => 'En charge (binaire, optionnel)', 'type' => 'cmd_info'],
                    ['key' => 'ev_cmd_plugged',  'd' => 'cmd_plugged',    'label' => 'Voiture branchée (binaire, optionnel)', 'type' => 'cmd_info'],
                    ['key' => 'ev_min_a',        'd' => 'min_a',          'label' => 'Intensité mini (A)', 'type' => 'number', 'default' => 6],
                    ['key' => 'ev_max_a',        'd' => 'max_a',          'label' => 'Intensité maxi (A)', 'type' => 'number', 'default' => 16],
                    ['key' => 'ev_phases',       'd' => 'phases',         'label' => 'Phases', 'type' => 'select', 'default' => '1',
                        'options' => ['1' => 'Monophasé', '3' => 'Triphasé']],
                    ['key' => 'ev_nominal_w',    'd' => 'nominal_w',      'label' => 'Puissance de charge si pas de réglage d\'intensité (W)', 'type' => 'number', 'default' => 3700],
                    ['key' => 'ev_batt_protect', 'd' => 'batt_protect',   'label' => 'Couper la décharge de la batterie maison si la voiture charge hors surplus', 'type' => 'checkbox', 'default' => 1],
                    ['key' => 'ev_cmd_batt_block',   'd' => 'cmd_batt_block',   'label' => 'Batterie : interdire la décharge (action)', 'type' => 'cmd_action'],
                    ['key' => 'ev_cmd_batt_unblock', 'd' => 'cmd_batt_unblock', 'label' => 'Batterie : autoriser la décharge (action)', 'type' => 'cmd_action'],
                    ['key' => 'ev_cmd_batt_discharge_slider', 'd' => 'cmd_batt_discharge_slider', 'label' => 'ou : Batterie décharge max (slider W, 0 = coupée)', 'type' => 'cmd_action'],
                    ['key' => 'ev_batt_discharge_normal_w', 'd' => 'batt_discharge_normal_w', 'label' => 'Décharge max normale (W, pour le slider)', 'type' => 'number', 'default' => 5000],
                ], self::planningFields('ev', 'spring,summer,autumn,winter')),
            ],
            'water_heater' => [
                'label'   => 'Chauffe-eau',
                'section' => 'water_heater',
                'fields'  => array_merge([
                    ['key' => 'wh_type',        'd' => 'wh_type',     'label' => 'Type de chauffe-eau', 'type' => 'select', 'default' => 'resistif',
                        'options' => ['resistif' => 'Électrique classique (délestage solaire)',
                                      'thermo'   => 'Thermodynamique (Arrêt la nuit, Auto le jour)']],
                    ['key' => 'wh_cmd_on',      'd' => 'cmd_on',      'label' => 'ON / Auto', 'type' => 'cmd_action'],
                    ['key' => 'wh_cmd_off',     'd' => 'cmd_off',     'label' => 'OFF / Arrêt', 'type' => 'cmd_action'],
                    ['key' => 'wh_night_stop',  'd' => 'night_stop',  'label' => 'Thermodynamique : arrêt à partir de (HH:MM)', 'type' => 'text', 'default' => '21:00'],
                    ['key' => 'wh_max_off_h',   'd' => 'max_off_h',   'label' => 'Thermodynamique : arrêt maximum (h)', 'type' => 'number', 'default' => 12],
                    ['key' => 'wh_dark_kwh',    'd' => 'dark_kwh',    'label' => 'Thermodynamique : pas d\'arrêt si le solaire de demain est sous (kWh)', 'type' => 'number', 'default' => 5],
                    ['key' => 'wh_cmd_power',   'd' => 'cmd_power',   'label' => 'Puissance (W)', 'type' => 'cmd_info'],
                    ['key' => 'wh_nominal_w',   'd' => 'nominal_w',   'label' => 'Puissance de chauffe (W)', 'type' => 'number', 'default' => 2000],
                    ['key' => 'wh_daily_wh',    'd' => 'daily_wh',    'label' => 'Besoin journalier estimé (Wh)', 'type' => 'number', 'default' => 3000],
                    ['key' => 'wh_min_surplus', 'd' => 'min_surplus', 'label' => 'Surplus mini (W)', 'type' => 'number', 'default' => 1000],
                    ['key' => 'wh_enabled',     'd' => 'wh_enabled',  'label' => 'Délestage solaire actif', 'type' => 'checkbox', 'default' => 0],
                ], self::planningFields('water_heater', 'spring,summer,autumn,winter')),
            ],
        ];
    }

    /* ==================================================================
     *  DÉPENDANCES (venv Python)
     * ================================================================== */

    private static function venvPython(): string {
        return realpath(__DIR__ . '/../../resources') . '/venv/bin/python3';
    }

    public static function dependancy_info() {
        $return = ['log' => log::getPathToLog(__CLASS__ . '_update'),
                   'progress_file' => jeedom::getTmpFolder(__CLASS__) . '/dependency'];
        if (file_exists($return['progress_file'])) {
            $return['state'] = 'in_progress';
            return $return;
        }
        $py = self::venvPython();
        $ok = is_executable($py)
            && trim(shell_exec(escapeshellarg($py) . ' -c "import aiohttp; print(1)" 2>/dev/null')) === '1';
        $return['state'] = $ok ? 'ok' : 'nok';
        return $return;
    }

    public static function dependancy_install() {
        log::remove(__CLASS__ . '_update');
        return [
            'script' => __DIR__ . '/../../resources/install_apt.sh ' . jeedom::getTmpFolder(__CLASS__) . '/dependency',
            'log'    => log::getPathToLog(__CLASS__ . '_update'),
        ];
    }

    /* ==================================================================
     *  DÉMON
     * ================================================================== */

    public static function deamon_info() {
        $return = ['log' => __CLASS__, 'state' => 'nok', 'launchable' => 'ok'];
        $pid_file = jeedom::getTmpFolder(__CLASS__) . '/deamon.pid';
        if (file_exists($pid_file)) {
            if (@posix_getsid(intval(trim(file_get_contents($pid_file))))) {
                $return['state'] = 'ok';
            } else {
                shell_exec(system::getCmdSudo() . 'rm -rf ' . $pid_file . ' 2>&1 > /dev/null');
            }
        }
        if (!is_executable(self::venvPython())) {
            $return['launchable'] = 'nok';
            $return['launchable_message'] = __('Dépendances non installées', __FILE__);
        } elseif (config::byKey('cmd_pv_power', __CLASS__, '') === '' || config::byKey('cmd_battery_soc', __CLASS__, '') === '') {
            $return['launchable'] = 'nok';
            $return['launchable_message'] = __('Configurer au minimum la puissance PV et le SOC batterie', __FILE__);
        }
        return $return;
    }

    public static function deamon_start() {
        self::deamon_stop();
        $info = self::deamon_info();
        if ($info['launchable'] != 'ok') {
            throw new Exception(__('Veuillez vérifier la configuration', __FILE__));
        }
        $cfgFile = self::writeConfigFile();
        $path = realpath(__DIR__ . '/../../resources/enersold');
        $cmd  = escapeshellarg(self::venvPython()) . ' ' . escapeshellarg($path . '/enersold.py');
        $cmd .= ' --loglevel '   . log::convertLogLevel(log::getLogLevel(__CLASS__));
        $cmd .= ' --socketport ' . intval(config::byKey('socketport', __CLASS__, self::DEFAULT_SOCKET_PORT));
        $cmd .= ' --callback '   . escapeshellarg(network::getNetworkAccess('internal', 'http:127.0.0.1:port:comp') . '/plugins/enerSol/core/php/jeeEnerSol.php');
        $cmd .= ' --apikey '     . escapeshellarg(jeedom::getApiKey(__CLASS__));
        $cmd .= ' --pid '        . escapeshellarg(jeedom::getTmpFolder(__CLASS__) . '/deamon.pid');
        $cmd .= ' --config '     . escapeshellarg($cfgFile);
        $cmd .= ' --datadir '    . escapeshellarg(self::dataDir());
        log::add(__CLASS__, 'info', 'Lancement du démon');
        exec($cmd . ' >> ' . log::getPathToLog(__CLASS__) . ' 2>&1 &');
        for ($i = 0; $i < 30; $i++) {
            if (self::deamon_info()['state'] == 'ok') break;
            sleep(1);
        }
        if ($i >= 30) {
            log::add(__CLASS__, 'error', __('Impossible de lancer le démon, vérifiez le log', __FILE__), 'unableStartDeamon');
            return false;
        }
        message::removeAll(__CLASS__, 'unableStartDeamon');
        return true;
    }

    public static function deamon_stop() {
        self::socketSend(['action' => 'stop']);
        $pid_file = jeedom::getTmpFolder(__CLASS__) . '/deamon.pid';
        if (file_exists($pid_file)) {
            $pid = intval(trim(file_get_contents($pid_file)));
            if ($pid > 0) system::kill($pid);
        }
        system::kill('enersold.py');
        sleep(1);
    }

    public static function dataDir(): string {
        $dir = __DIR__ . '/../../data';
        if (!is_dir($dir)) {
            mkdir($dir, 0775, true);
        }
        return realpath($dir);
    }

    /* ==================================================================
     *  CONSTRUCTION DE LA CONFIG POUR LE DÉMON
     * ================================================================== */

    private static function castValue(array $field, $raw) {
        $default = $field['default'] ?? '';
        $val = ($raw !== null && $raw !== '') ? $raw : $default;
        switch ($field['type']) {
            case 'number':   return is_numeric($val) ? floatval($val) : floatval($default ?: 0);
            case 'checkbox': return intval($val) !== 0;
            default:         return (string)$val;
        }
    }

    public static function buildConfig(): array {
        $global = [];
        foreach (self::globalSchema() as $fields) {
            foreach ($fields as $f) {
                $global[$f['key']] = self::castValue($f, config::byKey($f['key'], __CLASS__, ''));
            }
        }
        $global['meteo_latitude']  = floatval(config::byKey('info::latitude', 'core', 43.60) ?: 43.60);
        $global['meteo_longitude'] = floatval(config::byKey('info::longitude', 'core', 1.44) ?: 1.44);
        $devices = [];
        $schema  = self::deviceSchema();
        foreach (eqLogic::byType(__CLASS__) as $eq) {
            if ($eq->getConfiguration('eq_type') !== 'device') continue;
            $type = $eq->getConfiguration('device_type', '');
            if (!isset($schema[$type])) continue;
            $section = $schema[$type]['section'];
            $out = ['eq_id' => $eq->getId(), 'name' => $eq->getName(), 'device_type' => $type];
            $out['_info_keys'] = [];                 // seules ces commandes sont LUES par le démon
            foreach ($schema[$type]['fields'] as $f) {
                $out[$f['d']] = self::castValue($f, $eq->getConfiguration($f['key'], ''));
                if ($f['type'] === 'cmd_info') $out['_info_keys'][] = $f['d'];
            }
            if (!$eq->getIsEnable()) {
                $out['enabled'] = false;
            }
            $devices[$section] = $out;
        }
        return ['global' => $global, 'devices' => $devices];
    }

    public static function writeConfigFile(): string {
        $file = jeedom::getTmpFolder(__CLASS__) . '/config.json';
        file_put_contents($file, json_encode(self::buildConfig(), JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE));
        @chmod($file, 0640);
        return $file;
    }

    public static function sendConfigToDaemon() {
        self::writeConfigFile();
        self::socketSend(['action' => 'reload_config']);
    }

    /** Mode « ne rien faire » du lave-linge / sèche-linge (enregistre les cycles sans jamais lancer) */
    public static function setObserveOnly(string $type, bool $on): void {
        foreach (eqLogic::byType(__CLASS__) as $eq) {
            if ($eq->getConfiguration('eq_type') === 'device' && $eq->getConfiguration('device_type') === $type) {
                $eq->setConfiguration($type . '_observe_only', $on ? 1 : 0);
                $eq->save(true);
                $eq->checkAndUpdateCmd('ne_rien_faire', $on ? 1 : 0);
            }
        }
        if (self::deamon_info()['state'] == 'ok') {
            self::sendConfigToDaemon();
        }
    }

    /** Programmes appris par le démon pour le lave-linge / sèche-linge (fichier data/profiles_<type>.json) */
    public static function learnedProfiles(string $type): array {
        if (!in_array($type, ['washer', 'dryer'], true)) {
            return ['profiles' => [], 'cycles' => []];
        }
        $file = self::dataDir() . '/profiles_' . $type . '.json';
        $data = is_file($file) ? json_decode((string)file_get_contents($file), true) : null;
        if (!is_array($data)) {
            return ['profiles' => [], 'cycles' => []];
        }
        $out = [];
        foreach (($data['profiles'] ?? []) as $key => $p) {
            $parts = explode('|', $key, 2);
            $curve = $p['curve'] ?? [];
            $peak = $p['peak_w'] ?? ($curve ? max($curve) * 12 : null);
            $out[] = [
                'key' => $key,
                'program' => $parts[0],
                'option' => $parts[1] ?? '',
                'cycles' => intval($p['n'] ?? 0),
                'kwh' => round(floatval($p['energy_wh'] ?? 0) / 1000, 2),
                'duration_mn' => round(floatval($p['duration_mn'] ?? 0)),
                'peak_w' => $peak !== null ? round($peak) : null,
                'method' => $p['method'] ?? '',
                'curve_w' => array_map(function ($wh) { return round($wh * 12); }, $curve),
            ];
        }
        usort($out, function ($a, $b) { return strnatcasecmp($a['key'], $b['key']); });
        $cycles = array_slice(array_reverse($data['cycles'] ?? []), 0, 10);
        return ['profiles' => $out, 'cycles' => $cycles];
    }

    /** Renomme le programme d'un profil appris (via le démon, sinon directement dans le fichier) */
    public static function renameProfile(string $type, string $key, string $program): void {
        $program = trim($program);
        if (!in_array($type, ['washer', 'dryer'], true) || $program === '') {
            throw new Exception(__('Nom de programme invalide', __FILE__));
        }
        if (self::deamon_info()['state'] == 'ok'
            && self::socketSend(['action' => 'rename_profile', 'device' => $type, 'profile' => $key, 'program' => $program])) {
            usleep(500000);
            return;
        }
        $file = self::dataDir() . '/profiles_' . $type . '.json';
        $data = is_file($file) ? json_decode((string)file_get_contents($file), true) : null;
        if (!is_array($data) || !isset($data['profiles'][$key])) return;
        $parts = explode('|', $key, 2);
        $newKey = $program . (isset($parts[1]) && $parts[1] !== '' ? '|' . $parts[1] : '');
        if (isset($data['profiles'][$newKey])) {
            throw new Exception(__('Ce programme existe déjà : démarrez le démon pour fusionner les deux', __FILE__));
        }
        $data['profiles'][$newKey] = $data['profiles'][$key];
        unset($data['profiles'][$key]);
        foreach ($data['cycles'] as &$c) {
            $k = (($c['program'] ?? '') ?: 'defaut') . (($c['option'] ?? '') !== '' ? '|' . $c['option'] : '');
            if ($k === $key) $c['program'] = $program;
        }
        unset($c);
        file_put_contents($file, json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE));
    }

    /** Oublie un programme appris (via le démon s'il tourne, sinon directement dans le fichier) */
    public static function forgetProfile(string $type, string $key): void {
        if (!in_array($type, ['washer', 'dryer'], true)) {
            throw new Exception(__('Type d\'appareil invalide', __FILE__));
        }
        if (self::deamon_info()['state'] == 'ok'
            && self::socketSend(['action' => 'forget_profile', 'device' => $type, 'profile' => $key])) {
            usleep(500000);
            return;
        }
        $file = self::dataDir() . '/profiles_' . $type . '.json';
        $data = is_file($file) ? json_decode((string)file_get_contents($file), true) : null;
        if (!is_array($data) || !isset($data['profiles'][$key])) {
            return;
        }
        unset($data['profiles'][$key]);
        $data['cycles'] = array_values(array_filter($data['cycles'] ?? [], function ($c) use ($key) {
            $k = ($c['program'] ?: 'defaut') . (($c['option'] ?? '') !== '' ? '|' . $c['option'] : '');
            return $k !== $key;
        }));
        file_put_contents($file, json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE));
    }

    public static function socketSend(array $data): bool {
        $data['apikey'] = jeedom::getApiKey(__CLASS__);
        $port = intval(config::byKey('socketport', __CLASS__, self::DEFAULT_SOCKET_PORT));
        $sock = @socket_create(AF_INET, SOCK_STREAM, SOL_TCP);
        if ($sock === false) return false;
        socket_set_option($sock, SOL_SOCKET, SO_SNDTIMEO, ['sec' => 2, 'usec' => 0]);
        $ok = false;
        if (@socket_connect($sock, '127.0.0.1', $port)) {
            $ok = @socket_write($sock, json_encode($data) . "\n") !== false;
        }
        socket_close($sock);
        return $ok;
    }


    /* ==================================================================
     *  ÉCHANGES AVEC LE DÉMON (appelés par core/php/jeeEnerSol.php)
     * ================================================================== */

    /** Lecture groupée de commandes info */
    public static function readValues(array $ids): array {
        $out = [];
        foreach ($ids as $id) {
            $id = intval($id);
            if ($id <= 0) continue;
            $cmd = cmd::byId($id);
            if (!is_object($cmd)) { $out[$id] = null; continue; }
            if ($cmd->getType() !== 'info') {
                // sécurité : lire une commande ACTION l'exécuterait (ex. « Démarrer ») → jamais
                log::add(__CLASS__, 'warning', 'Commande ' . $cmd->getHumanName() . ' (#' . $id . ') configurée comme info mais c\'est une action : ignorée');
                $out[$id] = null;
                continue;
            }
            try {
                $out[$id] = $cmd->execCmd();
            } catch (Exception $e) {
                $out[$id] = null;
            }
        }
        return $out;
    }

    /** Exécution groupée de commandes action : [{id, options}] */
    public static function execCmds(array $list): array {
        $out = [];
        foreach ($list as $item) {
            $id = intval($item['id'] ?? 0);
            $cmd = cmd::byId($id);
            if (!is_object($cmd) || $cmd->getType() !== 'action') {
                $out[$id] = false;
                continue;
            }
            try {
                $opts = is_array($item['options'] ?? null) ? $item['options'] : [];
                $cmd->execCmd($opts);
                $out[$id] = true;
            } catch (Exception $e) {
                log::add(__CLASS__, 'warning', 'Exécution commande ' . $id . ' : ' . $e->getMessage());
                $out[$id] = false;
            }
        }
        return $out;
    }

    /** Historique moyenné par heure : {id: [[ 'Y-m-d H', moyenne ], ...]} */
    public static function hourlyHistory(array $ids, int $days): array {
        $days  = max(1, min(120, $days));
        $start = date('Y-m-d H:00:00', strtotime('-' . $days . ' days'));
        $end   = date('Y-m-d H:i:s');
        $out = [];
        foreach ($ids as $id) {
            $id = intval($id);
            if ($id <= 0) continue;
            $buckets = [];
            foreach (history::all($id, $start, $end) as $h) {
                $v = $h->getValue();
                if (!is_numeric($v)) continue;
                $k = substr($h->getDatetime(), 0, 13);
                if (!isset($buckets[$k])) $buckets[$k] = [0.0, 0];
                $buckets[$k][0] += floatval($v);
                $buckets[$k][1]++;
            }
            ksort($buckets);
            $series = [];
            foreach ($buckets as $k => $b) {
                $series[] = [$k, round($b[0] / $b[1], 2)];
            }
            $out[$id] = $series;
        }
        return $out;
    }

    /**
     * Moyennes horaires PONDÉRÉES PAR LE TEMPS (une valeur vaut jusqu'à la suivante, 2 h au plus),
     * sur une fenêtre [from_days ; to_days] jours en arrière. Sert à l'apprentissage du chauffage sur
     * plusieurs années (appelé par tranches de 90 jours pour ne pas charger Jeedom).
     */
    public static function hourlyHistoryWeighted(array $ids, int $fromDays, int $toDays, array $split = []): array {
        $fromDays = max(1, min(4000, $fromDays));
        $toDays   = max(0, min($fromDays - 1, $toDays));
        $start = date('Y-m-d H:00:00', strtotime('-' . $fromDays . ' days'));
        $end   = $toDays > 0 ? date('Y-m-d H:00:00', strtotime('-' . $toDays . ' days')) : date('Y-m-d H:i:s');
        $tEnd  = strtotime($end);
        $out = [];
        foreach ($ids as $id) {
            $id = intval($id);
            if ($id <= 0) continue;
            $pts = [];
            foreach (history::all($id, $start, $end) as $h) {
                $v = $h->getValue();
                if (is_numeric($v)) $pts[] = [strtotime($h->getDatetime()), floatval($v)];
            }
            usort($pts, function ($a, $b) { return $a[0] <=> $b[0]; });
            $acc = [];                                   // heure → [somme v·s, secondes, somme (part maison)·v·s]
            // chauffage zoné : part de la puissance qui chauffe TOUTE la maison (0 sous « bas », 1 au-dessus de « haut »)
            $lo = isset($split[$id]) ? floatval($split[$id][0]) : null;
            $hi = isset($split[$id]) ? floatval($split[$id][1]) : null;
            $n = count($pts);
            for ($i = 0; $i < $n; $i++) {
                $t = $pts[$i][0];
                $t2 = $i + 1 < $n ? $pts[$i + 1][0] : $tEnd;
                $t2 = min($t2, $t + 7200, $tEnd);
                while ($t < $t2) {
                    $hourStart = $t - ($t % 3600);
                    $seg = min($t2, $hourStart + 3600) - $t;
                    $k = date('Y-m-d H', $hourStart);
                    if (!isset($acc[$k])) $acc[$k] = [0.0, 0, 0.0];
                    $v = $pts[$i][1];
                    $acc[$k][0] += $v * $seg;
                    $acc[$k][1] += $seg;
                    if ($lo !== null && $hi > $lo) {
                        $share = max(0.0, min(1.0, ($v - $lo) / ($hi - $lo)));
                        $acc[$k][2] += $share * $v * $seg;
                    }
                    $t += $seg;
                }
            }
            ksort($acc);
            $series = []; $house = [];
            foreach ($acc as $k => $a) {
                if ($a[1] < 1800) continue;                              // heure couverte à moitié au moins
                $series[] = [$k, round($a[0] / $a[1], 2)];
                if ($lo !== null) $house[] = [$k, round($a[2] / $a[1], 2)];
            }
            $out[$id] = $series;
            if ($lo !== null) $out[$id . ':maison'] = $house;
        }
        return $out;
    }

    /** Notification via la commande configurée */
    public static function notify(string $title, string $message): bool {
        $id = config::byKey('cmd_notify', __CLASS__, '');
        if ($id === '') return false;
        $cmd = cmd::byId(intval($id));
        if (!is_object($cmd)) return false;
        try {
            $cmd->execCmd(['title' => $title, 'message' => $message]);
            return true;
        } catch (Exception $e) {
            log::add(__CLASS__, 'warning', 'Notification : ' . $e->getMessage());
            return false;
        }
    }

    /** Mise à jour des commandes info du maître */
    public static function applyEvents(array $values) {
        $master = self::getMaster();
        if (!is_object($master)) return;
        $apps = [];
        foreach (eqLogic::byType(__CLASS__) as $eq) {
            $t = $eq->getConfiguration('device_type');
            if ($eq->getConfiguration('eq_type') === 'device' && in_array($t, ['washer', 'dryer'], true)) $apps[$t] = $eq;
        }
        $map = ['conseil' => 'conseil', 'program' => 'programme', 'pilotage' => 'pilotage', 'running' => 'en_cours',
                'power' => 'puissance', 'last_cycle' => 'dernier_cycle', 'observe' => 'ne_rien_faire'];
        foreach ($values as $lid => $val) {
            $eq = $master;
            if (preg_match('/^(washer|dryer)_(.+)$/', $lid, $m)) {
                if (!isset($apps[$m[1]], $map[$m[2]])) continue;
                $eq = $apps[$m[1]];
                $lid = $map[$m[2]];
            }
            $cmd = $eq->getCmd('info', $lid);
            if (!is_object($cmd)) continue;
            if (is_array($val)) {
                $val = json_encode($val, JSON_UNESCAPED_UNICODE);
            } elseif (is_bool($val)) {
                $val = $val ? 1 : 0;
            }
            $cmd->event($val);
        }
    }

    /* ==================================================================
     *  ÉQUIPEMENTS
     * ================================================================== */

    public static function getMaster() {
        foreach (eqLogic::byType(__CLASS__) as $eq) {
            if ($eq->getConfiguration('eq_type', 'master') === 'master') return $eq;
        }
        return null;
    }

    /** Crée l'équipement principal (synthèse, suivi) s'il n'existe pas encore. */
    public static function ensureMaster() {
        if (self::getMaster() !== null) return;
        $m = new enerSol();
        $m->setName('enerSol');
        $m->setEqType_name(__CLASS__);
        $m->setConfiguration('eq_type', 'master');
        $m->setIsEnable(1);
        $m->setIsVisible(1);
        $m->save();
    }

    public static function masterCommands(): array {
        $info = [
            // [logicalId, nom, subType, unité, visible]
            ['pv_power',          'Production PV',                 'numeric', 'W', 1],
            ['battery_soc',       'SOC batterie',                  'numeric', '%', 1],
            ['load_power',        'Consommation maison',           'numeric', 'W', 1],
            ['grid_power',        'Puissance réseau',              'numeric', 'W', 1],
            ['available_power',   'Surplus disponible',            'numeric', 'W', 1],
            ['remaining_power',   'Surplus restant',               'numeric', 'W', 0],
            ['solar_start_h',     'Début plage solaire',           'numeric', 'h', 0],
            ['solar_end_h',       'Fin plage solaire',             'numeric', 'h', 0],
            ['in_solar_window',   'Plage solaire active',          'binary',  '',  0],
            ['pv_today_wh',       'Prévision PV aujourd\'hui',     'numeric', 'Wh', 1],
            ['pv_remain_wh',      'Prévision PV restante',         'numeric', 'Wh', 1],
            ['pv_j1_wh',          'Prévision PV J+1',              'numeric', 'Wh', 1],
            ['pv_j2_wh',          'Prévision PV J+2',              'numeric', 'Wh', 1],
            ['pv_j3_wh',          'Prévision PV J+3',              'numeric', 'Wh', 1],
            ['load_night_wh',     'Consommation prévue cette nuit','numeric', 'Wh', 1],
            ['soc_min_forecast',  'SOC mini prévu (48h)',          'numeric', '%', 1],
            ['soc_min_at',        'Heure du SOC mini prévu',       'string',  '',  1],
            ['soc_full_at',       'Heure de recharge complète prévue', 'string', '', 1],
            ['soc_forecast',      'Trajectoire SOC (JSON)',        'string',  '',  0],
            ['night_reserve_soc', 'Réserve de nuit',               'numeric', '%', 1],
            ['pv_accuracy',       'Précision prévision PV (hier)', 'numeric', '%', 1],
            ['conseils',          'Conseils J+1 à J+3',            'string',  '',  1],
            ['experience',        'Ce que j\'ai appris (compteurs)', 'string', '',  1],
            ['synthese',          'Ma journée',                    'string',  '',  1],
        ];
        // lave-linge et sèche-linge : leurs commandes sont sur leur propre équipement (applianceCommands)
        $devices = ['battery' => 'Batterie', 'pump' => 'Pompe piscine', 'pac_pool' => 'PAC piscine',
                    'pac_home' => 'PAC maison', 'water_heater' => 'Chauffe-eau', 'ev' => 'Voiture', 'vmi' => 'VMI'];
        foreach ($devices as $lid => $label) {
            $info[] = [$lid . '_running',  $label . ' — actif',     'binary',  '',  1];
            $info[] = [$lid . '_power',    $label . ' — puissance', 'numeric', 'W', 1];
            $info[] = [$lid . '_override', $label . ' — mode',      'string',  '',  0];
            $info[] = [$lid . '_decision', $label . ' — décision',  'string', '', 0];
        }
        $actions = [
            ['refresh',         'Rafraîchir'],
        ];
        foreach (array_keys($devices) as $lid) {
            foreach (['auto' => 'Auto', 'on' => 'ON', 'off' => 'OFF'] as $st => $stl) {
                $actions[] = [$lid . '_' . $st, $devices[$lid] . ' ' . $stl];
            }
        }
        return ['info' => $info, 'action' => $actions];
    }

    /** Commandes de l'équipement Lave-linge / Sèche-linge (sans préfixe : elles sont sur l'équipement) */
    public static function applianceCommands(): array {
        return [
            'info' => [
                ['conseil',       'Conseil enerSol',        'string',  '',  1],
                ['programme',     'Programme sélectionné',  'string',  '',  1],
                ['pilotage',      'Pilotage',               'string',  '',  1],
                ['en_cours',      'En cours',               'binary',  '',  1],
                ['puissance',     'Puissance',              'numeric', 'W', 1],
                ['dernier_cycle', 'Dernier cycle',          'string',  '',  1],
                ['ne_rien_faire', 'Ne rien faire',          'binary',  '',  0],
            ],
            'action' => [
                ['ne_rien_faire_on',  'Ne rien faire (conseil seulement)'],
                ['ne_rien_faire_off', 'Laisser enerSol lancer'],
                ['forcer',            'Lancer maintenant'],
            ],
        ];
    }

    public function postSave() {
        $type = $this->getConfiguration('eq_type', 'master');
        if ($type === 'master') {
            $def = self::masterCommands();
            $order = 0;
            foreach ($def['info'] as [$lid, $name, $sub, $unit, $vis]) {
                $this->ensureCmd($lid, $name, 'info', $sub, $unit, $vis, $order++);
            }
            foreach ($def['action'] as [$lid, $name]) {
                $this->ensureCmd($lid, $name, 'action', 'other', '', 0, $order++);
            }
        } elseif (in_array($this->getConfiguration('device_type'), ['washer', 'dryer'], true)) {
            $def = self::applianceCommands();
            $order = 0;
            foreach ($def['info'] as [$lid, $name, $sub, $unit, $vis]) {
                $this->ensureCmd($lid, $name, 'info', $sub, $unit, $vis, $order++);
            }
            foreach ($def['action'] as [$lid, $name]) {
                $this->ensureCmd($lid, $name, 'action', 'other', '', 1, $order++);
            }
            $type = $this->getConfiguration('device_type');
            $this->checkAndUpdateCmd('ne_rien_faire', $this->getConfiguration($type . '_observe_only', 0) ? 1 : 0);
        }
        if (self::deamon_info()['state'] == 'ok') {
            self::sendConfigToDaemon();
        }
    }

    public function preSave() {
        if ($this->getConfiguration('eq_type', '') === '') {
            $this->setConfiguration('eq_type', self::getMaster() === null ? 'master' : 'device');
        }
    }

    private function ensureCmd($lid, $name, $type, $sub, $unit, $visible, $order) {
        $cmd = $this->getCmd($type, $lid);
        if (is_object($cmd)) return;
        $cmd = new enerSolCmd();
        $cmd->setEqLogic_id($this->getId());
        $cmd->setLogicalId($lid);
        $cmd->setName($name);
        $cmd->setType($type);
        $cmd->setSubType($sub);
        $cmd->setUnite($unit);
        $cmd->setIsVisible($visible);
        $cmd->setOrder($order);
        if ($type === 'info' && $sub === 'numeric') {
            $cmd->setIsHistorized(1);
        }
        if ($lid === 'synthese') {
            $cmd->setTemplate('dashboard', 'enerSol::synthese');
            $cmd->setTemplate('mobile', 'enerSol::synthese');
            $cmd->setDisplay('showNameOndashboard', 0);
            $cmd->setDisplay('showNameOnmobile', 0);
        }
        $cmd->save();
    }

    public static function cron5() {
        if (config::byKey('deamonAutoMode', __CLASS__, 1) != 1) return;
        $info = self::deamon_info();
        if ($info['launchable'] == 'ok' && $info['state'] != 'ok') {
            log::add(__CLASS__, 'warning', 'Démon arrêté — relance');
            try { self::deamon_start(); } catch (Exception $e) { log::add(__CLASS__, 'error', $e->getMessage()); }
        }
    }
}

class enerSolCmd extends cmd {

    public function dontRemoveCmd() {
        return true;
    }

    public function execute($_options = []) {
        $lid = $this->getLogicalId();
        switch ($lid) {
            case 'refresh':
                enerSol::socketSend(['action' => 'refresh']);
                return;
        }
        $eq = $this->getEqLogic();
        $dtype = $eq->getConfiguration('device_type');
        if ($eq->getConfiguration('eq_type') === 'device' && in_array($dtype, ['washer', 'dryer'], true)) {
            if ($lid === 'ne_rien_faire_on' || $lid === 'ne_rien_faire_off') {
                enerSol::setObserveOnly($dtype, $lid === 'ne_rien_faire_on');
            } elseif ($lid === 'forcer') {
                enerSol::socketSend(['action' => 'set_override', 'key' => $dtype, 'state' => 'force']);
            }
            return;
        }
        if (preg_match('/^(battery|pump|pac_pool|pac_home|washer|dryer|water_heater|ev|vmi)_(auto|on|off|force)$/', $lid, $m)) {
            enerSol::socketSend(['action' => 'set_override', 'key' => $m[1], 'state' => $m[2]]);
        }
    }
}
