"use strict";
// Applied before the stylesheet so a saved dark theme does not flash white.
(()=>{
 const key='typelogic-theme',valid=new Set(['system','light','dark']);
 const media=window.matchMedia('(prefers-color-scheme: dark)');
 let preference='system';
 try{const saved=localStorage.getItem(key);if(valid.has(saved))preference=saved;}catch(_){/* Storage may be disabled; themes still work for this page. */}
 function apply(){
  document.documentElement.dataset.theme=preference==='system'?(media.matches?'dark':'light'):preference;
  window.dispatchEvent(new CustomEvent('typelogic-theme-change'));
 }
 window.TypeLogicTheme={get:()=>preference,set:value=>{
  if(!valid.has(value))return;
  preference=value;try{localStorage.setItem(key,value);}catch(_){}
  apply();
 }};
 media.addEventListener('change',()=>{if(preference==='system')apply();});
 window.addEventListener('storage',event=>{
  if(event.key===key||event.key===null){preference=valid.has(event.newValue)?event.newValue:'system';apply();}
 });
 apply();
})();
