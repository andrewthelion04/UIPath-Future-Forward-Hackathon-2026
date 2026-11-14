// Reproduce refresh-ul exact din extensie, cu raspunsuri capturate de fixture-ul Python.
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const PROJECT_ROOT=path.resolve(__dirname,'../..');
const source=fs.readFileSync(path.join(PROJECT_ROOT,'extension/entrypoints/sidepanel/App.tsx'),'utf8');
const body=source.split('const refresh = useCallback(async () => {')[1].split('}, []);')[0];
const fixture=JSON.parse(fs.readFileSync(path.join(os.tmpdir(),'audit_extension_race_fixture.json'),'utf8'));
let count=0; const release=[]; const gates=[new Promise(r=>release[0]=r),new Promise(r=>release[1]=r)];
const api=Object.fromEntries(['approvals','notifications','properties','incidents'].map(name=>[name,async()=>{const batch=Math.floor(count++/4);await gates[batch];return fixture[batch===0?'older':'newer'][name];}]));
const state={approvals:[],notifications:[],properties:[],incidents:[],error:null,loaded:false};
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
const refresh=new AsyncFunction('api','setApprovals','setNotifications','setProperties','setIncidents','setError','setLoaded',body);
const run=()=>refresh(api,...['approvals','notifications','properties','incidents','error','loaded'].map(key=>value=>{state[key]=value;}));
(async()=>{const older=run(),newer=run();release[1]();await newer;if(state.approvals.length!==0)throw new Error('Expected new state first');release[0]();await older;if(state.approvals.length!==1)throw new Error('Expected stale overwrite');console.log(JSON.stringify({newer_api_pending:fixture.newer.approvals.length,actual_final_component_pending:state.approvals.length,stale_approval_id:state.approvals[0].id,normal_path_status:fixture.normal_path_status,trailing_slash_concat_status:fixture.trailing_slash_concat_status},null,2));})();
