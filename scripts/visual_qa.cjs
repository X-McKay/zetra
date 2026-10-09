/* Browser QA for the generated offline HTML. Requires Playwright and Chrome. */
const fs = require('fs'), path = require('path');
const {chromium}=require(process.env.ZETRA_PLAYWRIGHT_PATH || 'playwright');
const root=path.resolve(__dirname,'..');
(async()=>{
 const out=path.join(root,'build/doc-qa');fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({headless:true,...(process.env.ZETRA_CHROME_PATH ? {executablePath:process.env.ZETRA_CHROME_PATH} : {channel:'chrome'})});
 const failures=[], results=[];
 for(const size of [{name:'desktop',width:1440,height:1000},{name:'mobile',width:390,height:844}]){
  const page=await browser.newPage({viewport:{width:size.width,height:size.height}});page.on('pageerror',e=>failures.push(e.message));
  for(const name of fs.readdirSync(path.join(root,'docs')).filter(n=>n.endsWith('.html')).map(n=>n.slice(0,-5)).sort()){
   await page.goto('file://'+path.join(root,'docs',name+'.html'));
   const layout=await page.evaluate(()=>({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,title:document.title,figures:document.querySelectorAll('figure').length,codeBlocks:document.querySelectorAll('pre').length,brokenImages:[...document.images].filter(i=>!i.complete||i.naturalWidth===0).length}));
   if(layout.scrollWidth>layout.width+1)failures.push(`${name}/${size.name}: body horizontal overflow ${layout.scrollWidth}/${layout.width}`);
   const clipped=await page.evaluate(()=>[...document.querySelectorAll('figure svg')].flatMap(svg=>{
    const bounds=svg.viewBox.baseVal;
    return [...svg.querySelectorAll('text')].filter(t=>{const b=t.getBBox();return b.x<bounds.x-1||b.y<bounds.y-1||b.x+b.width>bounds.x+bounds.width+1||b.y+b.height>bounds.y+bounds.height+1}).map(t=>t.textContent);
   }));
   if(clipped.length)failures.push(`${name}/${size.name}: SVG text outside viewBox: ${clipped.join('; ')}`);
   if(layout.brokenImages)failures.push(`${name}/${size.name}: broken images`);
   await page.screenshot({path:path.join(out,`${name}-${size.name}.png`)});
   if(name==='index'&&size.name==='desktop'){
    const figures=page.locator('figure');
    for(let i=0;i<await figures.count();i++)await figures.nth(i).screenshot({path:path.join(out,`figure-${i+1}.png`)});
   }
   if(name==='deployment'&&size.name==='desktop'){
    await page.locator('.boundary-widget').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'boundary-desktop.png')});
    await page.locator('.boundary-widget button').nth(2).click();const msg=await page.locator('.boundary-detail').innerText();if(!msg.includes('gateway authenticates'))failures.push('Boundary controls not responding');
   }
   if(name==='deployment-guide'){
    if(await page.locator('.guide-step').count()!==12)failures.push('Deployment guide must have twelve steps');
    if(size.name==='mobile')await page.locator('.mobile-toggle').click();
    await page.locator('#nav-search').fill('profile');
    if(await page.locator('.guide-nav nav a:visible').count()!==1)failures.push('Deployment specification navigation filter failed');
    await page.locator('#nav-search').fill('');
    await page.locator('.detail-toggle').click();
    if(await page.locator('.guide-detail:not([open]),.guide-example:not([open])').count())failures.push('Deployment guide expand-all failed');
    await page.locator('.detail-toggle').click();
    await page.locator('.guide-nav a[href="#deploy-step-6"]').click();
    await page.waitForFunction(()=>document.querySelector('.guide-nav a[href="#deploy-step-6"]')?.getAttribute('aria-current')==='step');
    if(!await page.locator('#reading-position').innerText().then(t=>t.endsWith('/ 12')))failures.push('Deployment guide step count incorrect');
    const first=page.locator('.guide-step[aria-labelledby="deploy-step-6"] details').first();
    await first.locator('summary').press('Enter');
    if(!await first.evaluate(e=>e.open))failures.push('Deployment guide keyboard disclosure failed');
    await first.locator('summary').press('Enter');
    await page.screenshot({path:path.join(out,`deployment-runtime-${size.name}.png`)});
    await page.evaluate(()=>{window.scrollTo({top:0,behavior:'instant'});document.querySelector('.sidebar').scrollTop=0;});
    await page.screenshot({path:path.join(out,`deployment-guide-${size.name}.png`)});
   }
   if(name==='lifecycle-guide'){
    if(await page.locator('.guide-hero,.guide-content>.toc').count())failures.push('Guide still has introductory content above step 1');
    if(!await page.locator('#dlc-step-1').innerText().then(t=>t.includes('Defining the problem before you build')))failures.push('Guide first-step title is incorrect');
    for(const key of ['purpose','information','result','limits']){
     await page.locator(`[data-brief="${key}"]`).click();
     if(await page.locator('[data-brief][aria-pressed="true"]').count()!==1||!await page.locator(`[data-brief-panel="${key}"]`).isVisible())failures.push(`Guide brief selection failed: ${key}`);
    }
    await page.locator('[data-brief="purpose"]').focus();
    await page.locator('[data-brief="purpose"]').press('Enter');
    if(!await page.locator('[data-brief-panel="purpose"]').isVisible())failures.push('Guide brief keyboard selection failed');
    await page.locator('.guide-step[aria-labelledby="dlc-step-1"]').evaluate(e=>e.scrollIntoView({block:'start',behavior:'instant'}));
    await page.locator('[data-brief="purpose"]').evaluate(e=>e.blur());
    await page.mouse.move(0,0);
    await page.screenshot({path:path.join(out,`guide-problem-${size.name}.png`)});
    if(size.name==='mobile')await page.locator('.mobile-toggle').click();
    await page.locator('#nav-search').fill('release');
    if(await page.locator('.guide-nav nav a:visible').count()!==1||await page.locator('.guide-nav .nav-section:not([hidden])').count()!==1)failures.push('Guide search did not filter navigation groups');
    await page.locator('#nav-search').fill('');
    await page.locator('.detail-toggle').click();
    if(await page.locator('.guide-detail:not([open]),.guide-example:not([open])').count())failures.push('Guide expand-all left details closed');
    await page.locator('.detail-toggle').click();
    if(await page.locator('.guide-detail[open],.guide-example[open]').count())failures.push('Guide collapse-all left details open');
    if(size.name==='mobile')await page.locator('.mobile-toggle').click();
    await page.locator('.repository-file summary').first().click();
    if(!await page.locator('.repository-file').first().evaluate(e=>e.open))failures.push('Repository file detail did not expand');
    await page.locator('.repository-file summary').first().press('Enter');
    if(await page.locator('.repository-file').first().evaluate(e=>e.open))failures.push('Repository file detail did not collapse with keyboard');
    await page.locator('.diagram-explanation summary').first().click();
    if(!await page.locator('.diagram-explanation').first().evaluate(e=>e.open))failures.push('Figure explanation did not expand');
    await page.locator('.diagram-explanation summary').first().press('Enter');
    const diagrams=page.locator('figure');
    for(let i=0;i<await diagrams.count();i++)await diagrams.nth(i).screenshot({path:path.join(out,`guide-figure-${i+1}-${size.name}.png`)});
    if(await page.locator('.guide-step').count()!==9)failures.push('Developer guide did not retain nine reading steps');
    await page.locator('.guide-example summary').first().click();
    if(!await page.locator('.guide-example').first().evaluate(e=>e.open))failures.push('Optional guide example did not expand');
    await page.locator('.guide-example summary').first().press('Enter');
    if(await page.locator('.guide-example').first().evaluate(e=>e.open))failures.push('Optional guide example did not collapse with keyboard');
    {
     if(size.name==='mobile')await page.locator('.mobile-toggle').click();
     await page.locator('.guide-nav a[href="#dlc-step-6"]').click();
     await page.waitForFunction(()=>document.querySelector('.guide-nav a[href="#dlc-step-6"]')?.getAttribute('aria-current')==='step');
     if(!page.url().endsWith('#dlc-step-6'))failures.push('Guide sidebar did not navigate to its step');
    }
    await page.screenshot({path:path.join(out,`guide-evaluation-${size.name}.png`)});
    await page.locator('.guide-step[aria-labelledby="dlc-step-3"] .diagram-explanation summary').click();
    await page.locator('.guide-step[aria-labelledby="dlc-step-3"]').evaluate(e=>e.scrollIntoView({block:'start',behavior:'instant'}));
    await page.waitForFunction(()=>document.querySelector('.guide-nav a[href="#dlc-step-3"]')?.getAttribute('aria-current')==='step');
    await page.screenshot({path:path.join(out,`guide-interaction-${size.name}.png`)});
   }
   results.push({document:name,viewport:size.name,...layout});
  }
  if(size.name==='mobile'){await page.locator('.mobile-toggle').click();if(!await page.locator('.sidebar').evaluate(e=>e.classList.contains('open')))failures.push('Mobile navigation toggle failed')}
  await page.close();
 }
 await browser.close();
 const report={checkedAt:new Date().toISOString(),status:failures.length?'failed':'passed',checks:['desktop/mobile rendered pages','body overflow','SVG text bounds','browser errors','inline assets','boundary interaction','mobile navigation','guide figure rendering and reading navigation','optional examples with keyboard','expand/collapse all details','repository and figure disclosures','brief selection with keyboard','guide starts at step 1','deployment guide navigation and keyboard disclosures'],results,failures};
 fs.writeFileSync(path.join(root,'docs/research/document-qa.json'),JSON.stringify(report,null,2)+'\n');
 console.log(JSON.stringify({status:report.status,pages:results.length,failures}));if(failures.length)process.exitCode=1;
})().catch(e=>{console.error(e);process.exitCode=1});
