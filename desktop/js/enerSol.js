/* enerSol — page équipements */

$('#table_cmd').sortable({axis: 'y', cursor: 'move', items: '.cmd', placeholder: 'ui-state-highlight', tolerance: 'intersect', forcePlaceholderSize: true})

function enerSolRefreshVisibility() {
  var isMaster = $('#sel_eqType').value() !== 'device'
  $('.ea-master-only').toggle(isMaster)
  $('.ea-device-only').toggle(!isMaster)
  var type = $('#sel_deviceType').value()
  $('.ea-type').hide()
  if (!isMaster && type) {
    $('.ea-type[data-type="' + type + '"]').show()
  }
  var learned = !isMaster && (type === 'washer' || type === 'dryer')
  $('.ea-learned').toggle(learned)
  if (learned) enerSolLoadProfiles(type)
}

/* ── Programmes appris (lave-linge / sèche-linge) ────────── */
function enerSolEsc(v) {
  return $('<div>').text(v === null || v === undefined ? '' : String(v)).html()
}

var enerSolProgramFr = {synthetic: 'synthétique', cotton: 'coton', cottons: 'coton', mixed: 'mixte', delicate: 'délicat',
  delicates: 'délicat', wool: 'laine', quick: 'rapide', rapid: 'rapide', eco: 'éco', eco_40_60: 'éco 40-60', baby: 'bébé',
  shirts: 'chemises', jeans: 'jean', duvet: 'couette', rinse: 'rinçage', spin: 'essorage', refresh: 'rafraîchir',
  allergy: 'anti-allergie', hygiene: 'hygiène', dark: 'foncé', timed: 'minuté', extra_dry: 'extra sec',
  cupboard_dry: 'prêt à ranger', iron_dry: 'prêt à repasser'}

function enerSolPrettyProgram(name) {
  var k = String(name || '').trim()
  if (!k || k === 'defaut') return '{{programme inconnu}}'
  var raw = k
  k = k.toLowerCase()
  ;['iot_', 'wash_', 'dry_', 'tumble_', 'wm_', 'td_'].forEach(function (p) { if (k.indexOf(p) === 0) k = k.substr(p.length) })
  var nice = enerSolProgramFr[k] || k.replace(/_/g, ' ')
  return nice === raw ? nice : nice + ' <small class="text-muted">(' + enerSolEsc(raw) + ')</small>'
}

function enerSolDuration(mn) {
  mn = Math.round(Number(mn) || 0)
  return Math.floor(mn / 60) + 'h' + ('0' + (mn % 60)).slice(-2)
}

function enerSolSparkline(curve) {
  if (!curve || !curve.length) return '<span class="text-muted">{{pas encore de courbe}}</span>'
  var w = 220, h = 40, max = Math.max.apply(null, curve.concat([1]))
  var bw = w / curve.length
  var bars = ''
  for (var i = 0; i < curve.length; i++) {
    var bh = Math.max(1, curve[i] / max * (h - 2))
    var t = (i * 5) + ' min : ' + curve[i] + ' W'
    bars += '<rect x="' + (i * bw).toFixed(1) + '" y="' + (h - bh).toFixed(1) + '" width="' + Math.max(1, bw - 0.5).toFixed(1)
      + '" height="' + bh.toFixed(1) + '" fill="#f0ad4e"><title>' + t + '</title></rect>'
  }
  return '<svg width="' + w + '" height="' + h + '" style="background:rgba(128,128,128,0.08)">' + bars + '</svg>'
}

function enerSolRenderProfiles(type, r) {
  var $tb = $('#table_eaProfiles tbody').empty()
  if (!r.profiles.length) {
    $tb.append('<tr><td colspan="9" class="text-muted">{{Aucun programme appris pour le moment : les cycles seront enregistrés automatiquement à la fin de chaque lavage / séchage.}}</td></tr>')
  }
  r.profiles.forEach(function (p) {
    $tb.append('<tr>'
      + '<td><b>' + enerSolPrettyProgram(p.program) + '</b></td>'
      + '<td>' + (p.option ? enerSolEsc(p.option) + (/^\d+$/.test(p.option) ? ' °C' : '') : '<span class="text-muted">—</span>') + '</td>'
      + '<td>' + p.cycles + '</td>'
      + '<td>' + Number(p.kwh).toFixed(2) + ' kWh</td>'
      + '<td>' + enerSolDuration(p.duration_mn) + '</td>'
      + '<td>' + (p.peak_w ? p.peak_w + ' W' : '—') + '</td>'
      + '<td>' + enerSolSparkline(p.curve_w) + '</td>'
      + '<td><small>' + enerSolEsc(p.method) + '</small></td>'
      + '<td style="white-space:nowrap">'
      + '<a class="btn btn-xs btn-default ea-rename" data-key="' + enerSolEsc(p.key) + '" data-program="' + enerSolEsc(p.program) + '" title="{{Renommer le programme}}"><i class="fas fa-pencil-alt"></i></a> '
      + '<a class="btn btn-xs btn-danger ea-forget" data-key="' + enerSolEsc(p.key) + '" title="{{Oublier ce programme}}"><i class="fas fa-trash"></i></a></td>'
      + '</tr>')
  })
  var $tc = $('#table_eaCycles tbody').empty()
  if (!r.cycles.length) {
    $tc.append('<tr><td colspan="7" class="text-muted">{{Aucun cycle enregistré}}</td></tr>')
  }
  r.cycles.forEach(function (c) {
    $tc.append('<tr><td>' + enerSolEsc(c.date) + '</td><td>' + enerSolPrettyProgram(c.program) + '</td><td>' + enerSolEsc(c.option) + '</td>'
      + '<td>' + (c.energy_wh !== null && c.energy_wh !== undefined ? (c.energy_wh / 1000).toFixed(2) + ' kWh' : '—') + '</td>'
      + '<td>' + enerSolDuration(c.duration_mn) + '</td><td><small>' + enerSolEsc(c.method) + '</small></td>'
      + '<td>' + (c.by_plugin ? '{{oui}}' : '{{non}}') + '</td></tr>')
  })
  $('.ea-rename').off('click').on('click', function () {
    var key = $(this).data('key')
    var current = String($(this).data('program') || '')
    var known = {}
    r.profiles.forEach(function (x) { if (x.program && x.program !== 'defaut') known[x.program] = 1 })
    r.cycles.forEach(function (x) { if (x.program && x.program !== 'defaut') known[x.program] = 1 })
    var hint = Object.keys(known).length ? '<br><small>{{Programmes déjà vus}} : ' + Object.keys(known).map(enerSolEsc).join(', ') + '</small>' : ''
    bootbox.prompt({
      title: '{{Nom du programme (tel qu\'affiché par hOn, ex. iot_wash_synthetic)}}' + hint,
      value: current === 'defaut' ? '' : current,
      callback: function (val) {
        if (val === null || !String(val).trim()) return
        enerSolLoadProfiles(type, null, {key: key, program: String(val).trim()})
      }
    })
  })
  $('.ea-forget').off('click').on('click', function () {
    var key = $(this).data('key')
    bootbox.confirm('{{Oublier le programme appris}} « ' + enerSolEsc(key) + ' » {{et ses cycles ?}}', function (ok) {
      if (ok) enerSolLoadProfiles(type, key)
    })
  })
}

function enerSolLoadProfiles(type, forgetKey, rename) {
  var data = {action: 'getProfiles', type: type}
  if (forgetKey) data = {action: 'forgetProfile', type: type, key: forgetKey}
  if (rename) data = {action: 'renameProfile', type: type, key: rename.key, program: rename.program}
  $.ajax({
    type: 'POST',
    url: 'plugins/enerSol/core/ajax/enerSol.ajax.php',
    data: data,
    dataType: 'json',
    global: false,
    error: function (request, status, error) { handleAjaxError(request, status, error) },
    success: function (data) {
      if (data.state !== 'ok') {
        $.fn.showAlert({message: data.result, level: 'danger'})
        return
      }
      enerSolRenderProfiles(type, data.result)
    }
  })
}

$('#bt_enerSolProfilesRefresh').off('click').on('click', function () {
  enerSolLoadProfiles($('#sel_deviceType').value())
})

$('#sel_eqType, #sel_deviceType').off('change').on('change', enerSolRefreshVisibility)

/* ── Sélecteurs de commandes ─────────────────────────────── */
function enerSolShowCmdName(key) {
  var $hidden = $('.eqLogicAttr[data-l2key="' + key + '"]')
  var $name = $('.ea-cmd-name[data-for="' + key + '"]')
  var id = $hidden.value()
  if (!id) {
    $name.val('')
    return
  }
  $name.val('#' + id + '#')
  jeedom.cmd.getHumanCmdName({
    id: id,
    error: function () {},
    success: function (name) { $name.val(name) }
  })
}

$('.ea-pick').off('click').on('click', function () {
  var key = $(this).data('for')
  var type = $(this).data('type')
  jeedom.cmd.getSelectModal({cmd: {type: type}}, function (result) {
    var id = (result.cmd && result.cmd.id) ? result.cmd.id : String(result.human).replace(/#/g, '')
    $('.eqLogicAttr[data-l2key="' + key + '"]').value(id)
    $('.ea-cmd-name[data-for="' + key + '"]').val(result.human)
  })
})

$('.ea-clear').off('click').on('click', function () {
  var key = $(this).data('for')
  $('.eqLogicAttr[data-l2key="' + key + '"]').value('')
  $('.ea-cmd-name[data-for="' + key + '"]').val('')
})

/* ── Saisons ─────────────────────────────────────────────── */
$('.ea-season').off('change').on('change', function () {
  var key = $(this).data('for')
  var list = []
  $('.ea-season[data-for="' + key + '"]:checked').each(function () { list.push($(this).data('season')) })
  $('.eqLogicAttr[data-l2key="' + key + '"]').value(list.join(','))
})

function enerSolSyncSeasons() {
  $('.eqLogicAttr[data-l2key$="_seasons"]').each(function () {
    var key = $(this).data('l2key')
    var val = String($(this).value() || '').split(',')
    $('.ea-season[data-for="' + key + '"]').each(function () {
      $(this).prop('checked', val.indexOf($(this).data('season')) >= 0)
    })
  })
}

/* ── Hook Jeedom : après chargement d'un équipement ──────── */
function printEqLogic(_eqLogic) {
  var conf = _eqLogic.configuration || {}
  // valeurs par défaut des champs jamais enregistrés
  $('.eqLogicAttr[data-l1key="configuration"][data-default]').each(function () {
    var key = $(this).data('l2key')
    if (conf[key] === undefined || conf[key] === null) {
      var d = $(this).attr('data-default')
      if ($(this).attr('type') === 'checkbox') {
        $(this).prop('checked', d === '1')
      } else {
        $(this).value(d)
      }
    }
  })
  $('.eqLogicAttr[data-l1key="configuration"][type="number"]').each(function () {
    var key = $(this).data('l2key')
    if ((conf[key] === undefined || conf[key] === '') && $(this).attr('placeholder') !== undefined) {
      $(this).value($(this).attr('placeholder'))
    }
  })
  if (!conf.eq_type) {
    $('#sel_eqType').value('device')
  }
  enerSolSyncSeasons()
  $('.ea-cmd-name').each(function () { enerSolShowCmdName($(this).data('for')) })
  enerSolRefreshVisibility()
}

/* ── Actions de la page ──────────────────────────────────── */
/* ── Tableau des commandes ───────────────────────────────── */
function addCmdToTable(_cmd) {
  if (!isset(_cmd)) _cmd = {configuration: {}}
  if (!isset(_cmd.configuration)) _cmd.configuration = {}
  var tr = '<tr class="cmd" data-cmd_id="' + init(_cmd.id) + '">'
  tr += '<td class="hidden-xs"><span class="cmdAttr" data-l1key="id"></span></td>'
  tr += '<td><input class="cmdAttr form-control input-sm" data-l1key="name" placeholder="{{Nom}}"></td>'
  tr += '<td><span class="cmdAttr" data-l1key="type"></span> / <span class="cmdAttr" data-l1key="subType"></span></td>'
  tr += '<td><span class="cmdAttr" data-l1key="htmlstate"></span></td>'
  tr += '<td>'
  tr += '<label class="checkbox-inline"><input type="checkbox" class="cmdAttr" data-l1key="isVisible" checked/>{{Afficher}}</label> '
  tr += '<label class="checkbox-inline"><input type="checkbox" class="cmdAttr" data-l1key="isHistorized"/>{{Historiser}}</label>'
  tr += '</td>'
  tr += '<td>'
  if (is_numeric(_cmd.id)) {
    tr += '<a class="btn btn-default btn-xs cmdAction" data-action="configure"><i class="fas fa-cogs"></i></a> '
    tr += '<a class="btn btn-default btn-xs cmdAction" data-action="test"><i class="fas fa-rss"></i> {{Tester}}</a>'
  }
  tr += '</td></tr>'
  $('#table_cmd tbody').append(tr)
  var $tr = $('#table_cmd tbody tr').last()
  $tr.setValues(_cmd, '.cmdAttr')
  jeedom.cmd.changeType($tr, init(_cmd.subType))
}
