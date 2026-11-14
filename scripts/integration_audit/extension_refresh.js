// Reproduce refresh-ul exact din extensie, cu raspunsuri capturate de fixture-ul Python.
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const PROJECT_ROOT=path.resolve(__dirname,'../..');
const source=fs.readFileSync(path.join(PROJECT_ROOT,'extension/entrypoints/sidepanel/App.tsx'),'utf8');
const body=source.split('const refresh = useCallback(async () => {')[1].split('}, []);')[0];
const fixture=JSON.parse(fs.readFileSync(path.join(os.tmpdir(),'audit_extension_fixture.json'),'utf8'));
const api=Object.fromEntries(Object.entries(fixture).map(([name,result])=>[name,async()=>{if(result.status>=400)throw new Error(result.body.detail);return result.body;}]));
const state={approvals:[],notifications:[],properties:[],incidents:[],error:null,loaded:false};
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
const refresh=new AsyncFunction('api','setApprovals','setNotifications','setProperties','setIncidents','setError','setLoaded',body);
refresh(api,...['approvals','notifications','properties','incidents','error','loaded'].map(key=>value=>{state[key]=value;})).then(()=>{
 if(state.approvals.length!==0 || state.properties.length!==0 || state.error!=='Not Found' || !state.loaded)throw new Error('Unexpected result');
 console.log(JSON.stringify({actual_component_refresh_state:state,expected_usable_approvals:fixture.approvals.body.length,expected_usable_properties:fixture.properties.body.length},null,2));
});
