/* Reading navigation for the standalone guide; no network or storage. */
(()=>{
 const steps=[...document.querySelectorAll('.guide-step')];
 const links=[...document.querySelectorAll('.guide-nav nav a')];
 const progress=document.querySelector('.reading-progress span');
 const label=document.querySelector('#reading-position');
 const details=[...document.querySelectorAll('.guide-content .guide-detail,.guide-content .guide-example')];
 const detailToggle=document.querySelector('.detail-toggle');
 function detailState(){
  const expanded=details.length>0&&details.every(d=>d.open);
  if(detailToggle){detailToggle.textContent=expanded?'Collapse all details':'Expand all details';detailToggle.setAttribute('aria-expanded',String(expanded));}
  track();
 }
 detailToggle?.addEventListener('click',()=>{const expand=!details.every(d=>d.open);details.forEach(d=>d.open=expand);detailState();});
 details.forEach(d=>d.addEventListener('toggle',detailState));
 let printClosed=[];
 addEventListener('beforeprint',()=>{printClosed=details.filter(d=>!d.open);details.forEach(d=>d.open=true);});
 addEventListener('afterprint',()=>{printClosed.forEach(d=>d.open=false);printClosed=[];detailState();});
 function activate(step){
  if(!step)return;
  const id=step.getAttribute('aria-labelledby'),index=steps.indexOf(step);
  links.forEach(a=>{const active=a.getAttribute('href')==='#'+id;a.classList.toggle('active',active);if(active)a.setAttribute('aria-current','step');else a.removeAttribute('aria-current');});
  if(label)label.textContent=`STEP ${String(index+1).padStart(2,'0')} / 09`;
 }
 function track(){
  const line=innerHeight*.25;
  activate([...steps].reverse().find(s=>s.getBoundingClientRect().top<=line)||steps[0]);
  const total=document.documentElement.scrollHeight-innerHeight;
  if(progress)progress.style.width=`${total>0?Math.min(100,Math.max(0,scrollY/total*100)):0}%`;
 }
 document.querySelector('#nav-search')?.addEventListener('input',()=>{document.querySelectorAll('.guide-nav .nav-section').forEach(group=>group.hidden=![...group.querySelectorAll('a')].some(a=>!a.hidden));});
 let pending=false;
 addEventListener('scroll',()=>{if(!pending){pending=true;requestAnimationFrame(()=>{track();pending=false;});}},{passive:true});
 addEventListener('resize',track);track();
})();
