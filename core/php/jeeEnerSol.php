<?php
/* Point d'entrée HTTP du démon enerSol (lecture/écriture groupées). */

require_once __DIR__ . '/../../../../core/php/core.inc.php';

header('Content-Type: application/json; charset=utf-8');

$raw  = file_get_contents('php://input');
$data = json_decode($raw, true);
if (!is_array($data)) {
    http_response_code(400);
    echo json_encode(['error' => 'invalid json']);
    exit;
}
if (!jeedom::apiAccess($data['apikey'] ?? '', 'enerSol')) {
    http_response_code(401);
    echo json_encode(['error' => 'unauthorized']);
    exit;
}

try {
    switch ($data['action'] ?? '') {
        case 'ping':
            $result = ['ok' => true];
            break;
        case 'read':
            $result = ['values' => enerSol::readValues((array)($data['cmds'] ?? []))];
            break;
        case 'exec':
            $result = ['results' => enerSol::execCmds((array)($data['cmds'] ?? []))];
            break;
        case 'history_weighted':
            set_time_limit(300);
            $result = ['history' => enerSol::hourlyHistoryWeighted((array)($data['cmds'] ?? []),
                                                                    intval($data['from_days'] ?? 90), intval($data['to_days'] ?? 0),
                                                                    (array)($data['split'] ?? []))];
            break;
        case 'history':
            $result = ['history' => enerSol::hourlyHistory((array)($data['cmds'] ?? []), intval($data['days'] ?? 30))];
            break;
        case 'event':
            enerSol::applyEvents((array)($data['values'] ?? []));
            $result = ['ok' => true];
            break;
        case 'notify':
            $result = ['ok' => enerSol::notify((string)($data['title'] ?? 'enerSol'), (string)($data['message'] ?? ''))];
            break;
        default:
            http_response_code(400);
            $result = ['error' => 'unknown action'];
    }
    echo json_encode($result, JSON_UNESCAPED_UNICODE);
} catch (Exception $e) {
    http_response_code(500);
    echo json_encode(['error' => $e->getMessage()]);
}
