#!/usr/bin/env node
// Kitchen dediee au flux AgentWatch. Aucun adaptateur de journal natif ni hook ajoute.
import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

async function main(){
const options = {port:4780};
for(let i=2;i<process.argv.length;i+=2){
  const key=process.argv[i],value=process.argv[i+1];
  if(!['--package','--project','--state-dir','--port'].includes(key)||!value)throw new Error('Expected --package, --project, --state-dir and optional --port.');
  options[key.slice(2)]=value;
}
if(!options.package||!options.project||!options['state-dir'])throw new Error('Provide --package, --project and --state-dir.');
const root=await fs.realpath(path.resolve(options.project)),stateDir=path.resolve(options['state-dir']),port=Number(options.port);
if(!(await fs.stat(root)).isDirectory()||!Number.isInteger(port)||port<1024||port>65515)throw new Error('Invalid project or port.');
let registration;
try{registration=JSON.parse(await fs.readFile(path.join(stateDir,'server.json'),'utf8'));}catch{}
if(registration){
  if(!Number.isInteger(registration.port)||registration.port<1024||registration.port>65535||!/^[a-f0-9]{48}$/.test(registration.hookToken))throw new Error('Invalid Kitchen registration.');
  const base=`http://127.0.0.1:${registration.port}`;
  let response;
  try{response=await fetch(base+'/api/state',{redirect:'error',signal:AbortSignal.timeout(1500)});}catch(e){
    if(!['ECONNREFUSED','ECONNRESET'].includes(e.cause?.code))throw new Error('Existing Kitchen is not responding; retry after it recovers.');
  }
  if(response){
    const state=await response.json();
    if(state.app!=='agenttrail-kitchen'||state.version!==2)throw new Error('Registered service is not a compatible Kitchen.');
    if(state.observing)throw new Error('Use a separate --state-dir for the AgentWatch-only Kitchen.');
    const attached=await fetch(base+'/api/attach',{method:'POST',headers:{authorization:`Bearer ${registration.hookToken}`,'content-type':'application/json'},body:JSON.stringify({projects:[root]}),redirect:'error',signal:AbortSignal.timeout(15000)});
    if(!attached.ok)throw new Error('Could not attach the project to Kitchen.');
    console.log(base);
    return;
  }
}
const {startOffice}=await import(pathToFileURL(path.join(path.resolve(options.package),'src/server.mjs')).href);
const office=await startOffice({roots:[root],home:path.join(stateDir,'observer-home'),stateDir,port,observe:false});
console.log(office.url);
let closing=false;
async function close(){if(closing)return;closing=true;await office.close();process.stdin.pause();process.stdin.unref?.();}
for(const signal of ['SIGINT','SIGTERM'])process.once(signal,()=>close().catch(error=>{console.error(error.message);process.exitCode=1;}));
process.stdin.resume();
process.stdin.on('data',chunk=>{if(chunk.toString().trim()==='stop')close().catch(error=>{console.error(error.message);process.exitCode=1;});});
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});
