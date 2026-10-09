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
  for(const name of ['index','strategy','developer','deployment','governance','toolkit','source-review']){
   await page.goto('file://'+path.join(root,'docs',name+'.html'));
   const layout=await page.evaluate(()=>({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,title:document.title,figures:document.querySelectorAll('figure').length,codeBlocks:document.querySelectorAll('pre').length,brokenImages:[...document.images].filter(i=>!i.complete||i.naturalWidth===0).length}));
   if(layout.scrollWidth>layout.width+1)failures.push(`${name}/${size.name}: body horizontal overflow ${layout.scrollWidth}/${layout.width}`);
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
   results.push({document:name,viewport:size.name,...layout});
  }
  if(size.name==='mobile'){await page.locator('.mobile-toggle').click();if(!await page.locator('.sidebar').evaluate(e=>e.classList.contains('open')))failures.push('Mobile navigation toggle failed')}
  await page.close();
 }
 await browser.close();
 const report={checkedAt:new Date().toISOString(),status:failures.length?'failed':'passed',checks:['desktop/mobile rendered pages','body overflow','browser errors','inline assets','boundary interaction','mobile navigation'],results,failures};
 fs.writeFileSync(path.join(root,'docs/research/document-qa.json'),JSON.stringify(report,null,2)+'\n');
 console.log(JSON.stringify({status:report.status,pages:results.length,failures}));if(failures.length)process.exitCode=1;
})().catch(e=>{console.error(e);process.exitCode=1});
