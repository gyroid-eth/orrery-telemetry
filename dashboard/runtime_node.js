/* The sole global observation -> existing network renderer adapter. */
(function(){'use strict';
function adapt(roster,observations){
 const ids=new Set(roster.map(a=>a.agent_id)),rows=new Map(observations.map(o=>[o.agent_id,o]));
 const parents=new Set();for(const o of rows.values())if(o.parent_state==='known'&&ids.has(o.parent_agent_id))parents.add(o.parent_agent_id);
 return new Map(roster.map(a=>{
  const o=rows.get(a.agent_id),child=o?.parent_state==='known'&&ids.has(o.parent_agent_id),parent=parents.has(a.agent_id);
  const live=o?.liveness==='live'&&o?.session_count===1&&['work','wait','ask','question'].includes(o?.act_state);
  const absent=o?.liveness==='absent';
  const label=a.retired_at?'RETIRED':absent?'NOT RUNNING':live?o.act_state.toUpperCase():o?.reason==='MULTIPLE_LIVE_SESSIONS'?'MULTIPLE SESSIONS':'ACTIVITY UNKNOWN';
  return [a.agent_id,{...a,retired:!!a.retired_at,state:live?'run':absent?'gone':'unknown',running:live,
   role:parent&&child?'both':parent?'parent':child?'child':'none',actState:live?o.act_state:null,
   ctxUsed:live&&typeof o.ctx_used==='number'?o.ctx_used:null,annot:{role:label}}];
 }));
}
window.OrreryRuntimeNode=Object.freeze({version:1,adapt});
})();
