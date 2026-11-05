<?php
require_once __DIR__ . '/../../../core/php/core.inc.php';
include_file('core', 'authentification', 'php');
if (!isConnect()) {
    include_file('desktop', '404', 'php');
    die();
}
?>
<form class="form-horizontal" id="enerSolConfig">
    <fieldset>
        <div class="alert alert-info">
            {{Ajoutez vos appareils sur la page du plugin, puis renseignez ici les mesures globales.}}
        </div>
        <?php foreach (enerSol::globalSchema() as $section => $fields) { ?>
            <legend><i class="fas fa-angle-right"></i> <?php echo htmlspecialchars($section); ?></legend>
            <?php if ($section === enerSol::HEAT_TABLE_SECTION) { ?>
                <div class="form-group"><div class="col-md-offset-1 col-md-10">
                    <p class="help-block">{{Ce que vous savez de votre maison, par température extérieure. Une case remplie est utilisée telle quelle ; une case vide prend la valeur apprise (historique et compteurs). Entre deux températures, enerSol interpole.}}</p>
                    <table class="table table-condensed table-bordered" style="max-width:640px">
                        <thead><tr><th>{{Dehors}}</th><th>{{kWh de chauffage pour gagner 1 °C}}</th><th>{{Descente sans chauffer (°C/h, maison à 20 °C)}}</th></tr></thead>
                        <tbody>
                        <?php foreach (enerSol::HEAT_TABLE_TEMPS as $t) { $tag = $t < 0 ? 'm' . abs($t) : (string)$t; ?>
                            <tr><td style="vertical-align:middle"><b><?php echo $t; ?> °C</b></td>
                                <td><input type="number" step="any" min="0" class="configKey form-control input-sm" data-l1key="heat_kwh_<?php echo $tag; ?>" placeholder="{{appris}}"/></td>
                                <td><input type="number" step="any" min="0" class="configKey form-control input-sm" data-l1key="heat_drop_<?php echo $tag; ?>" placeholder="{{appris}}"/></td></tr>
                        <?php } ?>
                        </tbody>
                    </table>
                    <a class="btn btn-default btn-sm" id="bt_eaHeatLearned"><i class="fas fa-download"></i> {{Reprendre les valeurs apprises}}</a>
                    <span id="ea_heat_msg" style="margin-left:8px"></span>
                </div></div>
                <?php continue; } ?>
            <?php foreach ($fields as $f) {
                $key = htmlspecialchars($f['key']);
                $label = htmlspecialchars($f['label']);
                $def = htmlspecialchars((string)($f['default'] ?? ''));
                echo '<div class="form-group"><label class="col-md-4 control-label">' . $label . '</label><div class="col-md-6">';
                switch ($f['type']) {
                    case 'cmd_info':
                    case 'cmd_action':
                        $t = $f['type'] === 'cmd_info' ? 'info' : 'action';
                        echo '<div class="input-group">'
                            . '<input type="text" class="form-control ea-gname roundedLeft" data-for="' . $key . '" readonly placeholder="{{Aucune commande}}"/>'
                            . '<input type="hidden" class="configKey ea-gid" data-l1key="' . $key . '"/>'
                            . '<span class="input-group-btn">'
                            . '<a class="btn btn-default ea-gpick" data-for="' . $key . '" data-type="' . $t . '"><i class="fas fa-list-alt"></i></a>'
                            . '<a class="btn btn-default ea-gclear roundedRight" data-for="' . $key . '"><i class="fas fa-times"></i></a>'
                            . '</span></div>';
                        break;
                    case 'select':
                        echo '<select class="configKey form-control" data-l1key="' . $key . '">';
                        foreach ($f['options'] as $v => $l) {
                            echo '<option value="' . htmlspecialchars($v) . '"' . ($v == ($f['default'] ?? '') ? ' selected' : '') . '>' . htmlspecialchars($l) . '</option>';
                        }
                        echo '</select>';
                        break;
                    case 'checkbox':
                        echo '<input type="checkbox" class="configKey" data-l1key="' . $key . '"/>';
                        break;
                    case 'password':
                        echo '<input type="password" class="configKey form-control" data-l1key="' . $key . '" autocomplete="new-password"/>';
                        break;
                    case 'number':
                        echo '<input type="number" step="any" class="configKey form-control" data-l1key="' . $key . '" placeholder="' . $def . '"/>';
                        break;
                    default:
                        echo '<input type="text" class="configKey form-control" data-l1key="' . $key . '" placeholder="' . $def . '"/>';
                }
                echo '</div></div>';
            }
        } ?>
    </fieldset>
</form>

<script>
  (function () {
    function showName(key) {
      var id = $('.ea-gid[data-l1key="' + key + '"]').value()
      var $n = $('.ea-gname[data-for="' + key + '"]')
      if (!id) { $n.val(''); return }
      $n.val('#' + id + '#')
      jeedom.cmd.getHumanCmdName({id: id, error: function () {}, success: function (name) { $n.val(name) }})
    }
    $('.ea-gpick').off('click').on('click', function () {
      var key = $(this).data('for')
      jeedom.cmd.getSelectModal({cmd: {type: $(this).data('type')}}, function (result) {
        var id = (result.cmd && result.cmd.id) ? result.cmd.id : String(result.human).replace(/#/g, '')
        $('.ea-gid[data-l1key="' + key + '"]').value(id)
        $('.ea-gname[data-for="' + key + '"]').val(result.human)
      })
    })
    $('.ea-gclear').off('click').on('click', function () {
      var key = $(this).data('for')
      $('.ea-gid[data-l1key="' + key + '"]').value('')
      $('.ea-gname[data-for="' + key + '"]').val('')
    })
    $('#bt_eaHeatLearned').off('click').on('click', function () {
      $.ajax({type: 'POST', url: 'plugins/enerSol/core/ajax/enerSol.ajax.php', data: {action: 'learnedHeatTable'},
        dataType: 'json', global: false,
        error: function (r, s, e) { handleAjaxError(r, s, e) },
        success: function (d) {
          if (d.state !== 'ok') { $('#ea_heat_msg').text(d.result); return }
          var r = d.result || {}, n = 0
          $.each(r.values || {}, function (k, v) { $('.configKey[data-l1key="' + k + '"]').value(v); n++ })
          if (!n) {
            $('#ea_heat_msg').html('<span class="label label-warning">{{Rien d\'appris pour l\'instant (démon démarré ? historique importé ?)}}</span>')
            return
          }
          var r2 = String(r.r2).replace('.', ',')
          var html = '<span class="label label-' + (r.fiable ? 'success' : 'warning') + '">' + n + ' {{valeurs reprises}} ('
            + r.heures + ' {{heures de chauffe, précision r²}} = ' + r2 + ')</span>'
          if (!r.fiable) html += '<br><span class="label label-danger">{{Précision faible : vérifiez chaque chiffre avant de sauvegarder}}</span>'
          if ((r.missing || []).length) html += '<br><span class="label label-default">{{Pas assez de mesures à}} '
            + r.missing.join(', ') + ' °C : {{laissez vide (enerSol prend la valeur la plus proche) ou mettez la vôtre}}</span>'
          $('#ea_heat_msg').html(html)
        }})
    })
    setTimeout(function () {
      $('.ea-gname').each(function () { showName($(this).data('for')) })
    }, 1200)
  })()

  function enerSol_postSaveConfiguration() {
    $.ajax({type: 'POST', url: 'plugins/enerSol/core/ajax/enerSol.ajax.php', data: {action: 'reloadConfig'}, dataType: 'json'})
  }
</script>
