<?php
if (!isConnect('admin')) {
    throw new Exception('{{401 - Accès non autorisé}}');
}
$plugin = plugin::byId('enerSol');
sendVarToJS('eqType', $plugin->getId());
$schema = enerSol::deviceSchema();
$eqLogics = eqLogic::byType($plugin->getId());

function enerSol_field_html(array $f): string {
    $key = htmlspecialchars($f['key']);
    $label = htmlspecialchars($f['label']);
    $attr = 'class="eqLogicAttr form-control" data-l1key="configuration" data-l2key="' . $key . '"';
    $html = '<div class="form-group"><label class="col-sm-4 control-label">' . $label . '</label><div class="col-sm-6">';
    switch ($f['type']) {
        case 'cmd_info':
        case 'cmd_action':
            $t = $f['type'] === 'cmd_info' ? 'info' : 'action';
            $html .= '<div class="input-group">'
                . '<input type="text" class="form-control ea-cmd-name roundedLeft" data-for="' . $key . '" readonly placeholder="{{Aucune commande}}"/>'
                . '<input type="hidden" ' . $attr . ' />'
                . '<span class="input-group-btn">'
                . '<a class="btn btn-default ea-pick" data-for="' . $key . '" data-type="' . $t . '" title="{{Choisir}}"><i class="fas fa-list-alt"></i></a>'
                . '<a class="btn btn-default ea-clear roundedRight" data-for="' . $key . '" title="{{Effacer}}"><i class="fas fa-times"></i></a>'
                . '</span></div>';
            break;
        case 'number':
            $html .= '<input type="number" step="any" ' . $attr . ' placeholder="' . htmlspecialchars((string)($f['default'] ?? '')) . '"/>';
            break;
        case 'checkbox':
            $html .= '<input type="checkbox" class="eqLogicAttr" data-l1key="configuration" data-l2key="' . $key . '"'
                . (!empty($f['default']) ? ' data-default="1"' : '') . '/>';
            break;
        case 'select':
            $html .= '<select ' . $attr . '>';
            foreach ($f['options'] as $v => $l) {
                $html .= '<option value="' . htmlspecialchars($v) . '">' . htmlspecialchars($l) . '</option>';
            }
            $html .= '</select>';
            break;
        case 'seasons':
            $html .= '<input type="hidden" ' . $attr . ' data-default="' . htmlspecialchars($f['default'] ?? '') . '"/>';
            foreach (['spring' => '{{Printemps}}', 'summer' => '{{Été}}', 'autumn' => '{{Automne}}', 'winter' => '{{Hiver}}'] as $s => $sl) {
                $html .= '<label class="checkbox-inline"><input type="checkbox" class="ea-season" data-for="' . $key . '" data-season="' . $s . '"/>' . $sl . '</label>';
            }
            break;
        default:
            $html .= '<input type="text" ' . $attr . ' placeholder="' . htmlspecialchars((string)($f['default'] ?? '')) . '"/>';
    }
    return $html . '</div></div>';
}
?>

<div class="row row-overflow">
    <div class="col-xs-12 eqLogicThumbnailDisplay">
        <legend><i class="fas fa-cog"></i> {{Gestion}}</legend>
        <div class="eqLogicThumbnailContainer">
            <div class="cursor eqLogicAction logoPrimary" data-action="add">
                <i class="fas fa-plus-circle"></i><br><span>{{Ajouter}}</span>
            </div>
            <div class="cursor eqLogicAction logoSecondary" data-action="gotoPluginConf">
                <i class="fas fa-wrench"></i><br><span>{{Configuration}}</span>
            </div>
        </div>
        <legend><i class="fas fa-solar-panel"></i> {{Mes équipements}}</legend>
        <div class="input-group" style="margin:5px;">
            <input class="form-control roundedLeft" placeholder="{{Rechercher}}" id="in_searchEqlogic"/>
            <div class="input-group-btn"><a id="bt_resetSearch" class="btn roundedRight" style="width:30px"><i class="fas fa-times"></i></a></div>
        </div>
        <div class="eqLogicThumbnailContainer">
            <?php foreach ($eqLogics as $eqLogic) {
                $opacity = ($eqLogic->getIsEnable()) ? '' : 'disableCard';
                $type = $eqLogic->getConfiguration('eq_type', 'master') === 'master'
                    ? '{{Maître}}' : ($schema[$eqLogic->getConfiguration('device_type')]['label'] ?? '?');
                echo '<div class="eqLogicDisplayCard cursor ' . $opacity . '" data-eqLogic_id="' . $eqLogic->getId() . '">';
                echo '<img src="' . $plugin->getPathImgIcon() . '"/><br>';
                echo '<span class="name">' . $eqLogic->getHumanName(true, true) . '</span>';
                echo '<span class="hiddenAsCard displayTableRight hidden">' . $type . '</span>';
                echo '</div>';
            } ?>
        </div>
    </div>

    <div class="col-xs-12 eqLogic" style="display:none;">
        <div class="input-group pull-right" style="display:inline-flex;">
            <span class="input-group-btn">
                <a class="btn btn-sm btn-default eqLogicAction roundedLeft" data-action="configure"><i class="fas fa-cogs"></i><span class="hidden-xs"> {{Configuration avancée}}</span></a>
                <a class="btn btn-sm btn-success eqLogicAction" data-action="save"><i class="fas fa-check-circle"></i> {{Sauvegarder}}</a>
                <a class="btn btn-sm btn-danger eqLogicAction roundedRight" data-action="remove"><i class="fas fa-minus-circle"></i> {{Supprimer}}</a>
            </span>
        </div>
        <ul class="nav nav-tabs" role="tablist">
            <li role="presentation"><a href="#" class="eqLogicAction" aria-controls="home" role="tab" data-toggle="tab" data-action="returnToThumbnailDisplay"><i class="fas fa-arrow-circle-left"></i></a></li>
            <li role="presentation" class="active"><a href="#eqlogictab" aria-controls="home" role="tab" data-toggle="tab"><i class="fas fa-tachometer-alt"></i> {{Équipement}}</a></li>
            <li role="presentation"><a href="#commandtab" aria-controls="home" role="tab" data-toggle="tab"><i class="fas fa-list"></i> {{Commandes}}</a></li>
        </ul>
        <div class="tab-content">
            <div role="tabpanel" class="tab-pane active" id="eqlogictab">
                <form class="form-horizontal">
                    <fieldset>
                        <legend><i class="fas fa-wrench"></i> {{Général}}</legend>
                        <div class="form-group">
                            <label class="col-sm-4 control-label">{{Nom}}</label>
                            <div class="col-sm-6">
                                <input type="text" class="eqLogicAttr form-control" data-l1key="id" style="display:none;"/>
                                <input type="text" class="eqLogicAttr form-control" data-l1key="name" placeholder="{{Nom de l'équipement}}"/>
                            </div>
                        </div>
                        <div class="form-group">
                            <label class="col-sm-4 control-label">{{Objet parent}}</label>
                            <div class="col-sm-6">
                                <select class="eqLogicAttr form-control" data-l1key="object_id">
                                    <option value="">{{Aucun}}</option>
                                    <?php foreach ((jeeObject::buildTree(null, false)) as $object) {
                                        echo '<option value="' . $object->getId() . '">' . str_repeat('&nbsp;&nbsp;', $object->getConfiguration('parentNumber')) . $object->getName() . '</option>';
                                    } ?>
                                </select>
                            </div>
                        </div>
                        <div class="form-group">
                            <label class="col-sm-4 control-label">{{Options}}</label>
                            <div class="col-sm-6">
                                <label class="checkbox-inline"><input type="checkbox" class="eqLogicAttr" data-l1key="isEnable" checked/>{{Activer}}</label>
                                <label class="checkbox-inline"><input type="checkbox" class="eqLogicAttr" data-l1key="isVisible" checked/>{{Visible}}</label>
                            </div>
                        </div>
                        <div class="form-group">
                            <label class="col-sm-4 control-label">{{Rôle}}</label>
                            <div class="col-sm-6">
                                <select class="eqLogicAttr form-control" data-l1key="configuration" data-l2key="eq_type" id="sel_eqType">
                                    <option value="master">{{Maître (tableau de bord et commandes)}}</option>
                                    <option value="device">{{Appareil piloté}}</option>
                                </select>
                            </div>
                        </div>
                        <div class="form-group ea-device-only">
                            <label class="col-sm-4 control-label">{{Type d'appareil}}</label>
                            <div class="col-sm-6">
                                <select class="eqLogicAttr form-control" data-l1key="configuration" data-l2key="device_type" id="sel_deviceType">
                                    <option value="">{{Choisir…}}</option>
                                    <?php foreach ($schema as $type => $def) {
                                        echo '<option value="' . $type . '">' . htmlspecialchars($def['label']) . '</option>';
                                    } ?>
                                </select>
                            </div>
                        </div>

                        <div class="ea-master-only">
                            <legend><i class="fas fa-info-circle"></i> {{Équipement maître}}</legend>
                            <p class="col-sm-offset-2 col-sm-8 text-muted">
                                {{Le maître porte les commandes de suivi (prévisions, SOC prévu, conseils, ce qui est appris) et les commandes de pilotage. Les mesures globales se règlent dans la configuration du plugin.}}
                            </p>
                        </div>

                        <?php foreach ($schema as $type => $def) { ?>
                            <div class="ea-type ea-device-only" data-type="<?php echo $type; ?>" style="display:none;">
                                <legend><i class="fas fa-plug"></i> <?php echo htmlspecialchars($def['label']); ?></legend>
                                <?php foreach ($def['fields'] as $f) {
                                    echo enerSol_field_html($f);
                                } ?>
                            </div>
                        <?php } ?>

                        <div class="ea-learned ea-device-only" style="display:none;">
                            <legend>
                                <i class="fas fa-graduation-cap"></i> {{Programmes appris}}
                                <a class="btn btn-xs btn-default pull-right" id="bt_enerSolProfilesRefresh"><i class="fas fa-sync"></i> {{Actualiser}}</a>
                            </legend>
                            <p class="text-muted">{{Chaque cycle terminé est enregistré par programme et température. La courbe (puissance toutes les 5 min) sert à choisir l'heure de départ selon le solaire prévu.}}</p>
                            <div class="table-responsive">
                                <table class="table table-condensed table-bordered" id="table_eaProfiles">
                                    <thead><tr>
                                        <th>{{Programme}}</th><th>{{Température / option}}</th><th>{{Cycles}}</th>
                                        <th>{{Énergie}}</th><th>{{Durée}}</th><th>{{Pic}}</th><th>{{Courbe de puissance}}</th><th>{{Mesure}}</th><th></th>
                                    </tr></thead>
                                    <tbody></tbody>
                                </table>
                            </div>
                            <label class="control-label">{{Derniers cycles}}</label>
                            <div class="table-responsive">
                                <table class="table table-condensed" id="table_eaCycles">
                                    <thead><tr><th>{{Départ}}</th><th>{{Programme}}</th><th>{{Option}}</th><th>{{Énergie}}</th><th>{{Durée}}</th><th>{{Mesure}}</th><th>{{Lancé par enerSol}}</th></tr></thead>
                                    <tbody></tbody>
                                </table>
                            </div>
                        </div>
                    </fieldset>
                </form>
            </div>

            <div role="tabpanel" class="tab-pane" id="commandtab">
                <br/>
                <div class="table-responsive">
                    <table id="table_cmd" class="table table-bordered table-condensed">
                        <thead>
                            <tr>
                                <th class="hidden-xs" style="min-width:50px;width:70px;">ID</th>
                                <th style="min-width:200px;width:350px;">{{Nom}}</th>
                                <th>{{Type}}</th>
                                <th>{{Valeur}}</th>
                                <th style="min-width:260px;width:310px;">{{Options}}</th>
                                <th style="min-width:80px;width:200px;">{{Actions}}</th>
                            </tr>
                        </thead>
                        <tbody></tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
</div>

<?php include_file('desktop', 'enerSol', 'js', 'enerSol'); ?>
<?php include_file('core', 'plugin.template', 'js'); ?>
