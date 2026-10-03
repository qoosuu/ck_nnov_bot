// Run against tests/ui_server.py; no real Telegram token or messages are used.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const crypto = require('node:crypto');
const fs = require('node:fs');
const assert = require('node:assert/strict');
function signed(id=439275668) {
  const data={auth_date:String(Math.floor(Date.now()/1000)),user:JSON.stringify({id,first_name:'Илья',is_bot:false})};
  const secret=crypto.createHmac('sha256','WebAppData').update('123456:test-secret').digest();
  data.hash=crypto.createHmac('sha256',secret).update(Object.keys(data).sort().map(k=>k+'='+data[k]).join('\n')).digest('hex');
  return new URLSearchParams(data).toString();
}
(async()=>{
  fs.mkdirSync('previews',{recursive:true});
  const browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
  try {
    const context=await browser.newContext({viewport:{width:390,height:844},deviceScaleFactor:1});
    await context.route('https://telegram.org/js/telegram-web-app.js',route=>route.fulfill({contentType:'text/javascript',body:''}));
    await context.addInitScript(raw=>{window.Telegram={WebApp:{initData:raw,ready(){},expand(){}}};},signed());
    const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
    page.on('dialog',d=>d.accept());
    await page.goto('http://127.0.0.1:8765');await page.locator('.table-card').last().waitFor();
    assert.equal(await page.locator('.table-card').count(),17);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:'previews/mobile.png',fullPage:false});
    const table17=page.locator('.table-card').nth(16);await table17.locator('.add-booking').click();
    await page.locator('[name=guest_name]').fill('Тестовая бронь <script>');
    await page.locator('[name=start_time]').fill('01:00');await page.locator('[name=guests]').fill('7');await page.locator('[name=comment]').fill('Проверка ночной брони');
    await page.screenshot({path:'previews/booking-form.png',fullPage:false});
    await page.locator('#save-booking').click();await page.locator('#booking-dialog').waitFor({state:'hidden'});
    await table17.locator('.booking').waitFor();assert.match(await table17.innerText(),/01:00/);
    await table17.locator('.booking').click();await page.locator('[name=guests]').fill('8');await page.locator('#save-booking').click();await page.locator('#booking-dialog').waitFor({state:'hidden'});
    assert.match(await table17.innerText(),/8 чел/);
    await table17.locator('.booking').click();await page.locator('#delete-booking').click();await page.locator('#booking-dialog').waitFor({state:'hidden'});assert.equal(await table17.locator('.booking').count(),0);
    await page.locator('#admin-tab').click();await page.locator('.metric').first().waitFor();assert.equal(await page.locator('.metric').count(),3);
    await page.screenshot({path:'previews/admin.png',fullPage:false});
    await page.locator('#digest-time').fill('15:30');await page.getByRole('button',{name:'Сохранить время'}).click();
    await page.locator('#access-id').fill('555');await page.locator('#access-form').getByRole('button',{name:'Отключить'}).click();
    await page.getByText('555 · Доступ отключён',{exact:false}).waitFor();
    await page.locator('#send-digest').click();await page.locator('#toast').filter({hasText:'очередь'}).waitFor();
    await page.locator('#bookings-tab').click();await page.setViewportSize({width:1280,height:900});await page.evaluate(()=>window.scrollTo(0,0));await page.screenshot({path:'previews/desktop.png',fullPage:false});
    await page.locator('#date-jump').fill('2026-10-04');await page.locator('#date-jump').dispatchEvent('change');await page.getByText('Броней: 0 · Гостей: 0',{exact:true}).waitFor();assert.equal(await page.locator('.table-card').count(),17);
    const unauthorized=await browser.newContext();await unauthorized.route('https://telegram.org/js/telegram-web-app.js',r=>r.fulfill({contentType:'text/javascript',body:''}));const noauth=await unauthorized.newPage();await noauth.goto('http://127.0.0.1:8765');await noauth.getByText('Откройте «Бронирования»',{exact:false}).waitFor();assert.equal(await noauth.locator('.table-card').count(),0);
    const staff=await browser.newContext();await staff.route('https://telegram.org/js/telegram-web-app.js',r=>r.fulfill({contentType:'text/javascript',body:''}));await staff.addInitScript(raw=>window.Telegram={WebApp:{initData:raw,ready(){},expand(){}}},signed(777));const sp=await staff.newPage();await sp.goto('http://127.0.0.1:8765');await sp.locator('.table-card').last().waitFor();assert.equal(await sp.locator('#admin-tab').isVisible(),false);
    assert.deepEqual(errors,[]);console.log('PASS: 17 tables, mobile overflow, create/edit/delete overnight booking, owner settings/access/digest, date switch, unauthorized gate, staff view; no JS errors.');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
