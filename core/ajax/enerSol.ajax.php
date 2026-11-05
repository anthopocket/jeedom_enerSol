<?php
try {
    require_once __DIR__ . '/../../../../core/php/core.inc.php';
    include_file('core', 'authentification', 'php');
    if (!isConnect('admin')) {
        throw new Exception(__('401 - Accès non autorisé', __FILE__));
    }
    ajax::init();

    switch (init('action')) {
        case 'getProfiles':
            ajax::success(enerSol::learnedProfiles(init('type')));
            break;

        case 'renameProfile':
            enerSol::renameProfile(init('type'), init('key'), init('program'));
            ajax::success(enerSol::learnedProfiles(init('type')));
            break;

        case 'forgetProfile':
            enerSol::forgetProfile(init('type'), init('key'));
            ajax::success(enerSol::learnedProfiles(init('type')));
            break;

        case 'learnedHeatTable':
            ajax::success(enerSol::learnedHeatTable());
            break;

        case 'reloadConfig':
            enerSol::sendConfigToDaemon();
            ajax::success();
            break;
    }
    throw new Exception(__('Aucune méthode correspondante à', __FILE__) . ' : ' . init('action'));
} catch (Exception $e) {
    ajax::error(displayException($e), $e->getCode());
}
