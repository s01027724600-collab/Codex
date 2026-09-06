// Isolated Edge: --remote-debugging-port=9705. Interaction POSTs are mocked.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  const tab = await fetch('http://127.0.0.1:9705/json/new?about:blank', { method: 'PUT' }).then(r => r.json());
  const ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise(resolve => ws.addEventListener('open', resolve, { once: true }));
  let sequence = 0;
  const pending = new Map(), errors = [];
  ws.addEventListener('message', event => {
    const msg = JSON.parse(event.data);
    if (msg.method === 'Runtime.exceptionThrown') errors.push(msg.params.exceptionDetails);
    if (!msg.id) return;
    const task = pending.get(msg.id);
    if (!task) return;
    pending.delete(msg.id);
    clearTimeout(task.timeout);
    if (msg.error) task.reject(new Error(JSON.stringify(msg.error)));
    else task.resolve(msg.result);
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timeout = setTimeout(() => { pending.delete(id); reject(new Error('CDP timeout: ' + method)); }, 15000);
    pending.set(id, { resolve, reject, timeout });
    ws.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async expression => {
    const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  const until = async expression => {
    for (let i = 0; i < 70; i++) {
      if (await evaluate(expression)) return;
      await delay(100);
    }
    throw new Error('Timed out: ' + expression);
  };
  const rect = id => evaluate(`(() => {const r=$('${id}').getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,cx:r.x+r.width/2,cy:r.y+r.height/2};})()`);
  const points = new Map();
  const touch = async (type, id, x, y) => {
    // CDP touchEnd points identify contacts to end, not those still held.
    const ending = points.get(id);
    if (type === 'touchCancel') points.clear();
    else if (type === 'touchEnd') points.delete(id);
    else points.set(id, { x, y, radiusX: 3, radiusY: 3, force: 1, id });
    await send('Input.dispatchTouchEvent', { type, touchPoints: type === 'touchEnd' ? (ending ? [ending] : []) : Array.from(points.values()) });
  };
  const settle = () => until('!inputBusy && inputQueue.length===0 && activePosts===0');
  const resetCalls = async () => { await settle(); await evaluate('testCalls=[];maxActivePosts=0'); };
  const checkStopped = async label => {
    await settle();
    const calls = await evaluate('testCalls.length');
    await delay(250);
    assert.equal(await evaluate('testCalls.length'), calls, label);
  };
  const saveScreenshot = async filename => {
    const screenshot = await send('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(__dirname, filename), Buffer.from(screenshot.data, 'base64'));
  };
  let mockScript, preferences;

  try {
    await send('Page.enable');
    await send('Runtime.enable');
    await send('Emulation.setDeviceMetricsOverride', { width: 1920, height: 960, deviceScaleFactor: 1, mobile: false });
    await send('Page.bringToFront');
    preferences = await send('Page.addScriptToEvaluateOnNewDocument', { source: "if(location.origin==='http://127.0.0.1:7050'){localStorage.setItem('pointer-preview','on');localStorage.setItem('control-overlay','on');localStorage.removeItem('pad-mode');localStorage.removeItem('mode')}" });
    await send('Page.navigate', { url: 'http://127.0.0.1:7050/' });
    await until("typeof logged!=='undefined' && logged && $('pad') && $('dirUp') && !$('detailImage').classList.contains('hidden') && $('detailImage').width>0");
    await send('Page.removeScriptToEvaluateOnNewDocument', { identifier: preferences.identifier });
    preferences = null;
    const readLayout = () => evaluate(`(() => {const p=$('pad').getBoundingClientRect();return {width:innerWidth,height:innerHeight,pad:{x:p.x,y:p.y,width:p.width,height:p.height},coverage:p.width*p.height/(innerWidth*innerHeight),detailCanvas:[$('detailImage').width,$('detailImage').height],pageOverflow:document.documentElement.scrollWidth>innerWidth,verticalOverflow:document.documentElement.scrollHeight>innerHeight,screen:screenData};})()`);
    const layout = await readLayout();
    assert.equal(await evaluate('padMode'), 'absolute', 'fresh setup defaults to direct absolute blind touch');
    assert.equal(layout.pageOverflow, false);
    assert.equal(layout.verticalOverflow, false);
    assert.ok(layout.coverage > .9, 'blind pad should occupy over 90% of viewport');
    assert.deepEqual(layout.detailCanvas, [720, 420]);
    const up = await rect('dirUp'), left = await rect('dirLeft'), down = await rect('dirDown'), right = await rect('dirRight');
    assert.ok(Math.abs(up.cx - down.cx) < 2, 'up must sit directly above down');
    assert.ok(up.cy < down.cy && left.cx < down.cx && down.cx < right.cx);
    assert.ok(Math.abs(left.cy - down.cy) < 2 && Math.abs(down.cy - right.cy) < 2, 'left/down/right form one bottom row');
    assert.ok(left.cx < layout.width / 4 && down.cy > layout.height * .65, 'inverted T belongs at bottom-left');
    assert.ok((await rect('click')).cx > layout.width * .65, 'auxiliary actions belong at right');
    await saveScreenshot('7050-blind-16x8.png');
    console.log('PASS live 16:8 full blind pad + inverted-T layout:', JSON.stringify(layout));
    await send('Emulation.setDeviceMetricsOverride', { width: 1920, height: 1200, deviceScaleFactor: 1, mobile: false });
    await delay(120);
    const tall = await readLayout();
    assert.ok(tall.coverage > .9);
    assert.equal(tall.pageOverflow || tall.verticalOverflow, false);
    await saveScreenshot('7050-blind-16x10.png');
    console.log('PASS live 16:10 layout:', JSON.stringify(tall));
    await send('Emulation.setDeviceMetricsOverride', { width: 1920, height: 960, deviceScaleFactor: 1, mobile: false });

    mockScript = await send('Page.addScriptToEvaluateOnNewDocument', { source: `
      if(location.origin==='http://127.0.0.1:7050'){
      localStorage.setItem('pad-mode','relative');localStorage.setItem('control-overlay','on');localStorage.setItem('pointer-preview','on');
      const nativeFetch=window.fetch.bind(window);
      window.testScreen=${JSON.stringify(layout.screen)};
      window.testCursor={x:testScreen.x+800,y:testScreen.y+270};
      window.testCalls=[];window.inputDelay=70;window.previewReads=0;window.previewFailure=false;window.activePosts=0;window.maxActivePosts=0;
      window.fetch=async (url,options={})=>{
        const route=String(url);
        if(route==='/state')return new Response(JSON.stringify({ok:true,screen:testScreen,cursor:{...testCursor}}),{status:200});
        if(route==='/pointer-view'){
          previewReads++;
          if(previewFailure)return new Response(JSON.stringify({ok:true,screen:testScreen,crop:{x:0,y:0,width:720,height:420},overview:'data:image/jpeg;base64,AA==',detail:'data:image/jpeg;base64,AA=='}),{status:200});
          return nativeFetch(url,options);
        }
        if(options.method==='POST'){
          const data=JSON.parse(options.body||'{}');testCalls.push({route,data,time:performance.now()});
          activePosts++;maxActivePosts=Math.max(activePosts,maxActivePosts);
          await new Promise(resolve=>setTimeout(resolve,inputDelay));
          if(route==='/move'){
            if(data.mode==='relative'){testCursor.x+=data.dx*1.35;testCursor.y+=data.dy*1.35;}
            else{testCursor.x=testScreen.x+data.nx*(testScreen.width-1);testCursor.y=testScreen.y+data.ny*(testScreen.height-1);}
            testCursor.x=Math.max(testScreen.x,Math.min(testScreen.x+testScreen.width-1,testCursor.x));
            testCursor.y=Math.max(testScreen.y,Math.min(testScreen.y+testScreen.height-1,testCursor.y));
          }
          activePosts--;return new Response(JSON.stringify({ok:true,...testCursor}),{status:200});
        }
        return nativeFetch(url,options);
      };
      }
    ` });
    await send('Page.reload');
    await until("typeof logged!=='undefined' && logged && typeof testCalls!=='undefined' && !$('detailImage').classList.contains('hidden')");
    await send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
    const pad = await rect('pad'), px = pad.x + pad.width * .4, py = pad.y + pad.height * .5;
    await resetCalls();
    await touch('touchStart', 1, px, py);
    await delay(40);
    await touch('touchEnd', 1);
    await settle();
    assert.equal(await evaluate("testCalls.filter(t=>t.route==='/button' && t.data.action==='click').length"), 1, 'one blind-pad tap clicks once');
    assert.equal(await evaluate("testCalls.some(t=>t.route==='/move' || t.route==='/wheel')"), false, 'relative tap must not reposition');
    console.log('PASS blind relative tap clicks exactly once');
    await resetCalls();
    await touch('touchStart', 1, px, py);
    await touch('touchMove', 1, px + 90, py + 35);
    await touch('touchMove', 1, px + 150, py + 65);
    await touch('touchEnd', 1);
    await settle();
    const slid = await evaluate('testCalls');
    assert.ok(slid.some(t => t.route === '/move' && t.data.mode === 'relative'));
    assert.equal(slid.some(t => t.route === '/button'), false, 'sliding finger must not click on release');
    console.log('PASS blind relative slide moves without accidental click');
    await evaluate("$('absoluteMode').click()");
    await resetCalls();
    await touch('touchStart', 1, px, py);
    await delay(40);
    await touch('touchEnd', 1);
    await settle();
    const absolute = await evaluate('testCalls');
    const absoluteMove = absolute.findIndex(t => t.route === '/move' && t.data.mode === 'absolute');
    const absoluteClick = absolute.findIndex(t => t.route === '/button' && t.data.action === 'click');
    assert.ok(absoluteMove >= 0 && absoluteClick > absoluteMove, 'absolute positioning must finish before tap click');
    assert.ok(Math.abs(absolute[absoluteMove].data.nx - .4) < .01 && Math.abs(absolute[absoluteMove].data.ny - .5) < .01);
    await evaluate("$('relativeMode').click()");
    console.log('PASS optional absolute mode positions before click');
    await resetCalls();
    await touch('touchStart', 1, px, py);
    await touch('touchStart', 2, px + 110, py);
    await touch('touchMove', 1, px, py + 75);
    await touch('touchMove', 2, px + 110, py + 75);
    await touch('touchEnd', 2);
    await touch('touchEnd', 1);
    await settle();
    assert.ok(await evaluate("testCalls.some(t=>t.route==='/wheel')"), 'two pad fingers scroll');
    assert.equal(await evaluate("testCalls.some(t=>t.route==='/button')"), false, 'two-finger gesture must not click');
    console.log('PASS two pad fingers scroll without clicking');

    const direction = await rect('dirRight'), action = await rect('rightClick');
    await resetCalls();
    await touch('touchStart', 1, direction.cx, direction.cy);
    await delay(450);
    assert.ok(await evaluate("testCalls.filter(t=>t.route==='/move').length") >= 2, 'held direction repeats');
    await touch('touchStart', 2, action.cx, action.cy);
    await delay(60);
    await touch('touchEnd', 2);
    await until("testCalls.some(t=>t.route==='/button' && t.data.action==='rightclick')");
    const movesAtAction = await evaluate("testCalls.filter(t=>t.route==='/move').length");
    await delay(250);
    assert.ok(await evaluate("testCalls.filter(t=>t.route==='/move').length") > movesAtAction, 'left direction continues while right finger acts');
    assert.equal(await evaluate('inputQueue.length'), 0, 'held direction must not accumulate move backlog');
    await touch('touchEnd', 1);
    await checkStopped('direction release must stop repeats');
    assert.equal(await evaluate('maxActivePosts'), 1, 'all input remains serialized');
    assert.equal(await evaluate("testCalls.filter(t=>t.route==='/button' && t.data.action==='rightclick').length"), 1);
    assert.equal(await evaluate("testCalls.some(t=>t.route==='/wheel')"), false);
    console.log('PASS inverted-T hold + right action support simultaneous fingers');
    await resetCalls();
    await touch('touchStart', 1, px, py);
    await touch('touchMove', 1, px + 70, py + 25);
    await touch('touchStart', 2, action.cx, action.cy);
    await delay(40);
    await touch('touchEnd', 2);
    await touch('touchMove', 1, px + 110, py + 65);
    await touch('touchEnd', 1);
    await settle();
    assert.equal(await evaluate("testCalls.some(t=>t.route==='/wheel')"), false, 'auxiliary finger is not a second pad scrolling finger');
    assert.equal(await evaluate("testCalls.filter(t=>t.route==='/button' && t.data.action==='rightclick').length"), 1);
    assert.equal(await evaluate("testCalls.filter(t=>t.route==='/button' && t.data.action==='click').length"), 0);
    console.log('PASS pad + auxiliary fingers remain independent');

    for (const cancellation of ['cancel', 'blur', 'hide']) {
      await resetCalls();
      await touch('touchStart', 1, direction.cx, direction.cy);
      await delay(250);
      if (cancellation === 'cancel') await touch('touchCancel', 1);
      if (cancellation === 'blur') await evaluate("window.dispatchEvent(new Event('blur'))");
      if (cancellation === 'hide') await evaluate("$('overlayToggle').click()");
      await checkStopped(cancellation + ' must stop held direction');
      if (cancellation !== 'cancel') await touch('touchEnd', 1);
    }
    assert.deepEqual(await rect('pad'), pad, 'hiding helpers cannot resize the primary blind pad');
    assert.equal(await evaluate(`document.elementFromPoint(${direction.cx},${direction.cy})?.closest('#pad')?.id`), 'pad', 'hidden direction footprint returns to blind pad');
    assert.equal(await evaluate(`document.elementFromPoint(${action.cx},${action.cy})?.closest('#pad')?.id`), 'pad', 'hidden action footprint returns to blind pad');
    await resetCalls();
    await evaluate("$('dirRight').dispatchEvent(new PointerEvent('pointerdown',{pointerId:99,pointerType:'touch',button:0,bubbles:true}));$('rightClick').click()");
    await delay(350);
    assert.equal(await evaluate('testCalls.length'), 0, 'hidden auxiliary controls cannot issue remote input');
    await saveScreenshot('7050-blind-uncovered-16x8.png');
    await evaluate("$('overlayToggle').click()");
    console.log('PASS cancel/blur/hide stop directions; hidden helpers restore pad hit area');

    const detail = await rect('detailImage');
    assert.equal(await evaluate(`document.elementFromPoint(${detail.cx},${detail.cy})?.closest('#pad')?.id`), 'pad', 'mouse preview is transparent to hit testing');
    await until('!previewBusy && previewCrop!==null');
    await evaluate("clearTimeout(previewTimer);testCursor={x:previewCrop.x+previewCrop.width+80,y:previewCrop.y};applyCursor(testCursor)");
    const opacity = await evaluate("(() => {let n=$('detailImage'),a=1;while(n){a*=Number(getComputedStyle(n).opacity);n=n.parentElement;}return a;})()");
    assert.equal(opacity, 1, 'moving outside crop must not dim the image');
    const retainedFrame = await evaluate("$('detailImage').toDataURL()");
    await evaluate('previewFailure=true;clearTimeout(previewTimer);refreshPreview()');
    await until("!previewBusy && $('previewPanel').classList.contains('stale')");
    assert.equal(await evaluate("$('detailImage').toDataURL()"), retainedFrame, 'failed decode preserves old canvas pixels');
    await evaluate("previewFailure=false;$('previewToggle').click()");
    await delay(150);
    assert.equal(await evaluate("$('previewPanel').getBoundingClientRect().width===0 || getComputedStyle($('previewPanel')).visibility==='hidden'"), true, 'preview switch hides the entire panel');
    const pausedReads = await evaluate('previewReads');
    await delay(650);
    assert.equal(await evaluate('previewReads'), pausedReads, 'hidden preview stops image requests');
    assert.equal(await evaluate("$('detailImage').toDataURL()"), retainedFrame, 'hidden preview retains pixels for next opening');
    await evaluate("$('previewToggle').click()");
    await until('previewReads>' + pausedReads);
    console.log('PASS preview passes touches through, keeps last good frame and stops hidden traffic');

    await resetCalls();
    await evaluate("$('drag').click()");
    await settle();
    assert.ok(await evaluate('remoteButtonDown'));
    await evaluate("$('overlayToggle').click()");
    await settle();
    assert.equal(await evaluate('remoteButtonDown'), false, 'hiding controls releases dragging');
    assert.ok(await evaluate("testCalls.some(t=>t.route==='/release' || t.route==='/button' && t.data.action==='up')"));
    await evaluate("$('overlayToggle').click();$('drag').click()");
    await settle();
    await evaluate("inputDelay=200;testCalls=[];enqueueInput('/move',{mode:'absolute',nx:.5,ny:.5});$('drag').click();emergencyRelease()");
    await settle();
    await delay(250);
    assert.equal(await evaluate("testCalls.some(t=>t.route==='/release')"), true, 'cancellation releases even if release was queued');
    assert.equal(await evaluate('remoteButtonDown'), false);
    console.log('PASS drag is released when helpers hide and queued release survives cancellation');
    assert.equal(errors.length, 0, JSON.stringify(errors));
    console.log('All blind-pad browser checks passed. No real desktop input was sent.');
  } finally {
    if (mockScript) await send('Page.removeScriptToEvaluateOnNewDocument', { identifier: mockScript.identifier }).catch(() => {});
    if (preferences) await send('Page.removeScriptToEvaluateOnNewDocument', { identifier: preferences.identifier }).catch(() => {});
    await send('Page.navigate', { url: 'about:blank' }).catch(() => {});
    ws.close();
    await fetch('http://127.0.0.1:9705/json/close/' + tab.id).catch(() => {});
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
