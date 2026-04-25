
var all=[], dt=null, cDon=null, cBar=null;

function fv(v,d,s){
  if(v==null||isNaN(v))return'<span class=text-muted>--</span>';
  return(+v).toFixed(d)+(s||'');
}
function fpnl(v,d,s){
  if(v==null||isNaN(v))return'<span class=text-muted>--</span>';
  var c=v>0?'pnl-pos':v<0?'pnl-neg':'pnl-neu';
  return'<span class="'+c+'">'+(v>0?'+':'')+v.toFixed(d)+(s||'')+'</span>';
}
function frsi(v){
  if(v==null)return'<span class=text-muted>--</span>';
  var c=v>70?'text-danger fw-bold':v<30?'text-primary fw-bold':'text-success';
  return'<span class="'+c+'">'+v.toFixed(0)+'</span>';
}
function freco(r,col,icon){
  var bg={danger:'#dc3545',success:'#198754',warning:'#ffc107',secondary:'#6c757d'}[col]||'#6c757d';
  var tc=col==='warning'?'#000':'#fff';
  return'<span class="badge badge-reco" style="background:'+bg+';color:'+tc+'">'+icon+' '+r+'</span>';
}
function scColor(v){
  return v>=70?'#dc3545':v>=50?'#fd7e14':v>=30?'#ffc107':'#198754';
}
function fScore(v){
  if(v==null)return'<span class=text-muted>--</span>';
  var c=scColor(v);
  var bar='<div style="height:7px;background:#e9ecef;border-radius:4px"><div class=sc-bar-inner style="width:'+v+'%;background:'+c+'"></div></div>';
  return'<div class=sc-bar>'+bar+'<div class=sc-val style="color:'+c+'">'+v.toFixed(0)+'/100</div></div>';
}

function buildKPIs(data){
  var totalVal=data.reduce(function(s,d){return s+(d.valeur||0);},0);
  var totalInv=data.reduce(function(s,d){return s+(d.investi||0);},0);
  var totalPnl=totalVal-totalInv;
  var totalPct=totalInv>0?totalPnl/totalInv*100:0;
  var enProfit=data.filter(function(d){return(d.pnl_pct||0)>0;}).length;
  var urgents=data.filter(function(d){return d.reco_col==='danger';}).length;
  var surveiller=data.filter(function(d){return d.reco_col==='warning';}).length;
  var ks=[
    {l:'Valeur totale',v:totalVal.toLocaleString('fr-FR',{minimumFractionDigits:0})+'€',i:'💼',c:''},
    {l:'P&L total',v:(totalPnl>=0?'+':'')+totalPnl.toLocaleString('fr-FR',{minimumFractionDigits:0})+'€',
     i:totalPnl>=0?'📈':'📉',c:totalPnl>=0?'#d4edda':'#f8d7da'},
    {l:'Performance',v:(totalPct>=0?'+':'')+totalPct.toFixed(2)+'%',
     i:totalPct>=0?'✅':'⚠️',c:totalPct>=0?'#d4edda':'#f8d7da'},
    {l:'Positions',v:data.length+' ('+enProfit+' en profit)',i:'📋',c:''},
    {l:'Alertes vente',v:urgents,i:'🔴',c:urgents>0?'#f8d7da':''},
    {l:'À surveiller',v:surveiller,i:'🟡',c:surveiller>0?'#fff3cd':''},
  ];
  document.getElementById('kp').innerHTML=ks.map(function(k){
    return'<div class="col-6 col-md-4 col-lg-2"><div class="card text-center py-2"'+(k.c?' style="background:'+k.c+'"':'')+'>'+
      '<div style="font-size:1.4rem">'+k.i+'</div>'+
      '<div class="fw-bold fs-5">'+k.v+'</div>'+
      '<div class="text-muted small">'+k.l+'</div></div></div>';
  }).join('');
}

function buildAlerts(data){
  var urgents=data.filter(function(d){return d.reco_col==='danger';});
  var html='';
  if(urgents.length>0){
    html='<div class="alert alert-danger mb-0"><strong>🔴 Signaux de vente actifs :</strong> ';
    html+=urgents.map(function(d){
      return'<span class="badge bg-danger me-1" style="cursor:pointer;font-size:12px" onclick="showDetail(&#39;'+d.id+'&#39;)">'+d.ticker+' ('+d.pnl_pct.toFixed(1)+'%)</span>';
    }).join('');
    html+=' — cliquer pour voir le détail.</div>';
  }
  document.getElementById('alr').innerHTML=html;
}

var PIE_COLORS=['#1a3a5c','#e8a020','#198754','#dc3545','#0d6efd','#6f42c1','#fd7e14','#20c997','#6c757d','#d63384'];

function buildCharts(data){
  if(cDon)cDon.destroy();
  cDon=new Chart(document.getElementById('cD'),{type:'doughnut',
    data:{labels:data.map(function(d){return d.ticker;}),
    datasets:[{data:data.map(function(d){return d.valeur||0;}),
    backgroundColor:data.map(function(_,i){return PIE_COLORS[i%PIE_COLORS.length];}),
    borderWidth:2,borderColor:'#fff'}]},
    options:{responsive:true,maintainAspectRatio:false,
    plugins:{legend:{position:'right',labels:{font:{size:11},boxWidth:14}}}}});

  if(cBar)cBar.destroy();
  var sorted=data.slice().sort(function(a,b){return(b.pnl_pct||0)-(a.pnl_pct||0);});
  cBar=new Chart(document.getElementById('cB'),{type:'bar',
    data:{labels:sorted.map(function(d){return d.ticker;}),
    datasets:[{label:'P&L %',data:sorted.map(function(d){return d.pnl_pct||0;}),
    borderRadius:4,
    backgroundColor:sorted.map(function(d){
      return(d.pnl_pct||0)>=0?'rgba(25,135,84,.75)':'rgba(220,53,69,.75)';})}]},
    options:{responsive:true,maintainAspectRatio:false,
    plugins:{legend:{display:false}},
    scales:{y:{grid:{color:'#eee'},ticks:{callback:function(v){return v+'%';}}},
    x:{grid:{display:false}}}}});
}

function buildTable(data){
  document.getElementById('rc').textContent=data.length+' positions';
  if(dt){dt.destroy();dt=null;}
  var tb=document.getElementById('tbd'); tb.innerHTML='';
  data.forEach(function(d){
    var rowcl=d.reco_col==='danger'?'table-danger':'';
    tb.innerHTML+='<tr class="'+rowcl+'">'
      +'<td><code style="font-size:12px">'+d.ticker+'</code></td>'
      +'<td>'+d.nom+'</td>'
      +'<td class="text-center">'+d.quantite+'</td>'
      +'<td class="text-end">'+fv(d.prix_achat,2,'€')+'</td>'
      +'<td class="text-end">'+fv(d.prix,2,'€')+'</td>'
      +'<td class="text-end">'+fv(d.valeur,0,'€')+'</td>'
      +'<td class="text-end">'+fpnl(d.pnl_eur,0,'€')+'</td>'
      +'<td class="text-end">'+fpnl(d.pnl_pct,2,'%')+'</td>'
      +'<td class="text-end">'+(d.upside!=null?fpnl(d.upside,1,'%'):'<span class=text-muted>--</span>')+'</td>'
      +'<td class="text-center">'+frsi(d.rsi)+'</td>'
      +'<td class="text-center">'+fScore(d.sell_score)+'</td>'
      +'<td>'+freco(d.reco,d.reco_col,d.reco_icon)+'</td>'
      +'<td style="white-space:nowrap">'
        +'<button class="btn btn-outline-primary btn-sm me-1 py-0 px-2" onclick="showDetail(&#39;'+d.id+'&#39;)">🔍</button>'
        +'<button class="btn btn-outline-secondary btn-sm me-1 py-0 px-2" onclick="openEditModal(&#39;'+d.id+'&#39;)">✏️</button>'
        +'<button class="btn btn-outline-danger btn-sm py-0 px-2" onclick="deletePos(&#39;'+d.id+'&#39;)">🗑</button>'
      +'</td></tr>';
  });
  dt=$('#tb').DataTable({pageLength:25,order:[],
    language:{search:'Rechercher:',lengthMenu:'_MENU_/page',
    info:'_START_-_END_ sur _TOTAL_',paginate:{next:'›',previous:'‹'}}});
}

function buildUI(data){
  var empty=!data||data.length===0;
  document.getElementById('es').style.display=empty?'block':'none';
  document.getElementById('pc').style.display=empty?'none':'block';
  if(!empty){
    buildKPIs(data);buildAlerts(data);buildCharts(data);buildTable(data);
  }
  document.getElementById('ov').style.display='none';
  document.getElementById('mn').style.display='block';
}

function showDetail(id){
  var d=all.find(function(x){return x.id===id;}); if(!d)return;
  document.getElementById('mDetTitle').textContent='📊 '+d.nom+' ('+d.ticker+')';
  var html='<div class="row g-2 mb-3">';
  var items=[
    ['Prix achat',fv(d.prix_achat,2,'€')],
    ['Prix actuel',fv(d.prix,2,'€')],
    ['P&L',fpnl(d.pnl_eur,0,'€')+'<br>'+fpnl(d.pnl_pct,2,'%')],
    ['Valeur totale',fv(d.valeur,0,'€')],
    ['Cible analystes',d.cible?fv(d.cible,2,'€')+'<br><small class=text-muted>'+d.analystes+' analystes</small>':'<span class=text-muted>--</span>'],
    ['RSI',frsi(d.rsi)],
    ['P/E fwd',d.pe_fwd?d.pe_fwd+'x':'<span class=text-muted>--</span>'],
    ['ROIC',d.roic?d.roic.toFixed(1)+'%':'<span class=text-muted>--</span>'],
  ];
  items.forEach(function(it){
    html+='<div class="col-6 col-md-3"><div class="card text-center p-2">'
      +'<div class="text-muted small">'+it[0]+'</div>'
      +'<div class="fw-bold">'+it[1]+'</div></div></div>';
  });
  html+='</div>';
  html+='<div class="text-center mb-3">Recommandation : '+freco(d.reco,d.reco_col,d.reco_icon)+'</div>';
  if(d.sell_score!=null){
    var sc=d.sell_score,cc=scColor(sc);
    var lbl=sc>=70?'VENDRE MAINTENANT':sc>=50?'Envisager la vente':sc>=30?'Surveiller':'Conserver';
    html+='<div class="p-3 mb-3 rounded" style="background:#f8f9fa;border:1px solid #dee2e6">';
    html+='<div class="d-flex align-items-center gap-3 mb-2">';
    html+='<div style="font-size:2rem;font-weight:900;color:'+cc+'">'+sc.toFixed(0)+'</div>';
    html+='<div><div class="fw-bold" style="color:'+cc+'">Score de timing : '+lbl+'</div>';
    html+='<div style="height:10px;width:200px;background:#dee2e6;border-radius:5px;margin-top:4px">';
    html+='<div style="height:10px;width:'+sc+'%;background:'+cc+';border-radius:5px"></div></div></div></div>';
    if(d.sell_bd&&d.sell_bd.length){
      d.sell_bd.forEach(function(b){
        var pct=b.m>0?b.p/b.m*100:0;
        var bc=pct>=70?'#dc3545':pct>=40?'#fd7e14':'#198754';
        html+='<div class=sc-bd-row>'
          +'<div style="width:140px;color:#495057">'+b.l+'</div>'
          +'<div class=sc-bd-bar><div class=sc-bd-fill style="width:'+pct.toFixed(0)+'%;background:'+bc+'"></div></div>'
          +'<div style="width:50px;text-align:right;font-weight:600;color:'+bc+'">'+b.p+' / '+b.m+'</div></div>';
      });
    }
    html+='</div>';
  }
  html+='<h6 class="fw-bold mb-2">Signaux détaillés :</h6>';
  if(d.signals&&d.signals.length){
    d.signals.forEach(function(s){
      html+='<div class="sig-'+s.niveau+'">'
        +'<div class="fw-semibold">'+s.icone+' '+s.titre+'</div>'
        +'<div class="text-muted small mt-1">'+s.detail+'</div></div>';
    });
  }
  html+='<div class="mt-3 p-2 bg-light rounded small">'
    +'<strong>Paramètres :</strong>'
    +' Stop-loss : '+d.stop_loss+'%'
    +' | Take-profit : +'+d.take_profit+'%'
    +(d.date_achat?' | Achat le : '+d.date_achat:'')
    +(d.ma50?' | MA50 : '+d.ma50+'€':'')
    +(d.ma200?' | MA200 : '+d.ma200+'€':'')
    +' | Secteur : '+d.secteur+'</div>';
  document.getElementById('mDetBody').innerHTML=html;
  new bootstrap.Modal(document.getElementById('mDet')).show();
}

document.addEventListener('input',function(e){
  if(['fQty','fPrix'].includes(e.target.id)){
    var q=parseFloat(document.getElementById('fQty').value)||0;
    var p=parseFloat(document.getElementById('fPrix').value)||0;
    document.getElementById('fInvesti').value=q&&p?(q*p).toFixed(2)+' €':'';
  }
});

function openAddModal(){
  document.getElementById('fId').value='';
  document.getElementById('mPosTitle').textContent='Ajouter une position';
  ['fTicker','fNom','fDate','fQty','fPrix','fInvesti'].forEach(function(i){document.getElementById(i).value='';});
  document.getElementById('fSL').value='-15';
  document.getElementById('fTP').value='30';
  new bootstrap.Modal(document.getElementById('mPos')).show();
}

function openEditModal(id){
  var pos=all.find(function(x){return x.id===id;}); if(!pos)return;
  document.getElementById('fId').value=pos.id;
  document.getElementById('mPosTitle').textContent='Modifier '+pos.ticker;
  document.getElementById('fTicker').value=pos.ticker;
  document.getElementById('fNom').value=pos.nom||'';
  document.getElementById('fDate').value=pos.date_achat||'';
  document.getElementById('fQty').value=pos.quantite;
  document.getElementById('fPrix').value=pos.prix_achat;
  document.getElementById('fInvesti').value=(pos.quantite*pos.prix_achat).toFixed(2)+' €';
  document.getElementById('fSL').value=pos.stop_loss;
  document.getElementById('fTP').value=pos.take_profit;
  new bootstrap.Modal(document.getElementById('mPos')).show();
}

function savePosition(){
  var id=document.getElementById('fId').value;
  var tk=document.getElementById('fTicker').value.trim();
  var qty=document.getElementById('fQty').value;
  var px=document.getElementById('fPrix').value;
  if(!tk||!qty||!px){alert('Ticker, quantité et prix sont obligatoires.');return;}
  var body={ticker:tk,nom:document.getElementById('fNom').value,
    quantite:qty,prix_achat:px,date_achat:document.getElementById('fDate').value,
    stop_loss:document.getElementById('fSL').value,
    take_profit:document.getElementById('fTP').value};
  bootstrap.Modal.getInstance(document.getElementById('mPos')).hide();
  document.getElementById('ov').style.display='flex';
  document.getElementById('mn').style.display='none';
  document.getElementById('ob').style.width='0%';
  fetch(id?'/api/positions/'+id:'/api/positions',
    {method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(function(){startPoll();});
}

function deletePos(id){
  var d=all.find(function(x){return x.id===id;});
  if(!d||!confirm('Supprimer la position '+d.ticker+' ?'))return;
  fetch('/api/positions/'+id,{method:'DELETE'}).then(function(){
    document.getElementById('ov').style.display='flex';
    document.getElementById('mn').style.display='none';
    setTimeout(load,500);
  });
}

var _poll=null, _pollCount=0, _POLL_MAX=120;
function poll(){
  _pollCount++;
  if(_pollCount>_POLL_MAX){clearInterval(_poll);_poll=null;load();return;}
  fetch('/api/progress').then(function(r){return r.json();}).then(function(p){
    var pct=Math.round(p.done/Math.max(p.total,1)*100);
    document.getElementById('ob').style.width=pct+'%';
    document.getElementById('oc').textContent=p.done+' / '+p.total;
    document.getElementById('om').textContent=p.loading?'Analyse Yahoo Finance...':'Finalisation...';
    if(!p.loading){clearInterval(_poll);_poll=null;setTimeout(load,400);}
  }).catch(function(){clearInterval(_poll);_poll=null;setTimeout(load,1000);});
}

function startPoll(){
  if(_poll)clearInterval(_poll);
  _pollCount=0;
  _poll=setInterval(poll,1000);
}

function load(){
  fetch('/api/data').then(function(r){return r.json();}).then(function(d){
    if(d.loading){startPoll();return;}
    all=d.data||[];
    if(d.last_update)document.getElementById('lu').textContent='Mis à jour : '+d.last_update;
    buildUI(all);
  }).catch(function(err){
    console.error('Erreur /api/data:',err);
    setTimeout(load,3000);
  });
}

function doRefresh(){
  document.getElementById('ov').style.display='flex';
  document.getElementById('mn').style.display='none';
  document.getElementById('ob').style.width='0%';
  fetch('/api/refresh',{method:'POST'}).then(function(){startPoll();});
}

window.addEventListener('DOMContentLoaded',load);
