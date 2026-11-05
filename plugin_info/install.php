<?php
require_once __DIR__ . '/../../../core/php/core.inc.php';

function enerSol_install() {
    if (config::byKey('socketport', 'enerSol', '') === '') {
        config::save('socketport', enerSol::DEFAULT_SOCKET_PORT, 'enerSol');
    }
    enerSol_migrateFromEnergIA();
    enerSol::ensureMaster();
}

function enerSol_update() {
    foreach (eqLogic::byType('enerSol') as $eq) {
        $eq->save();
    }
}

function enerSol_remove() {
}

/**
 * Reprise de l'ancien plugin « energIA » (même code, ancien nom) : équipements et commandes
 * (mêmes numéros : scénarios, designs et historiques restent valables), réglages et données
 * apprises (cycles des machines, chauffage, chauffe-eau). Une seule fois, et seulement si
 * enerSol n'a encore aucun équipement.
 */
function enerSol_migrateFromEnergIA() {
    try {
        $old = DB::Prepare("SELECT COUNT(*) AS n FROM eqLogic WHERE eqType_name = 'energIA'", [], DB::FETCH_TYPE_ROW);
        $mine = DB::Prepare("SELECT COUNT(*) AS n FROM eqLogic WHERE eqType_name = 'enerSol'", [], DB::FETCH_TYPE_ROW);
        if (empty($old['n']) || !empty($mine['n'])) {
            return;
        }
        log::add('enerSol', 'info', 'Reprise de energIA : ' . $old['n'] . ' équipements');

        // 1) l'ancien démon ne doit plus tourner (même port)
        config::save('deamonAutoMode', 0, 'energIA');
        exec(system::getCmdSudo() . 'pkill -f "resources/energiad/energiad.py" 2>/dev/null');

        // 2) équipements et commandes : on change seulement leur type (les numéros ne bougent pas)
        DB::Prepare("UPDATE eqLogic SET eqType_name = 'enerSol' WHERE eqType_name = 'energIA'", []);
        DB::Prepare("UPDATE cmd SET eqType = 'enerSol' WHERE eqType = 'energIA'", []);
        DB::Prepare("UPDATE cmd SET template = REPLACE(template, 'energIA::', 'enerSol::') WHERE eqType = 'enerSol'", []);

        // 3) réglages du plugin (seulement ceux qu'enerSol connaît)
        $n = 0;
        foreach (enerSol::globalSchema() as $fields) {
            foreach ($fields as $f) {
                $v = config::byKey($f['key'], 'energIA', null);
                if ($v !== null && $v !== '') {
                    config::save($f['key'], $v, 'enerSol');
                    $n++;
                }
            }
        }
        log::add('enerSol', 'info', "Reprise de energIA : $n réglages");

        // 4) données apprises
        $src = __DIR__ . '/../../energIA/data';
        $dst = __DIR__ . '/../data';
        if (is_dir($src)) {
            if (!is_dir($dst)) mkdir($dst, 0775, true);
            foreach (glob($src . '/*.json') as $file) {
                $name = basename($file);
                if ($name === 'ai_state.json' || file_exists("$dst/$name")) continue;
                copy($file, "$dst/$name");
            }
            log::add('enerSol', 'info', 'Reprise de energIA : données apprises copiées');
        }

        // 5) anciennes commandes de l'IA (retirée)
        foreach (eqLogic::byType('enerSol') as $eq) {
            foreach ($eq->getCmd() as $cmd) {
                $lid = (string)$cmd->getLogicalId();
                if (strpos($lid, 'ai_') === 0 || preg_match('/_ai$/', $lid)) {
                    $cmd->remove();
                }
            }
            $eq->save();
        }
        log::add('enerSol', 'info', 'Reprise de energIA terminée : vous pouvez supprimer le plugin energIA');
    } catch (Exception $e) {
        log::add('enerSol', 'error', 'Reprise de energIA : ' . $e->getMessage());
    }
}
