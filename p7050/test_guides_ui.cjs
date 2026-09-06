// Run against an isolated Edge on port 9705. Every interaction POST is mocked.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  const tab = await fetch('http://127.0.0.1:9705/json/new?about:blank', { method: 'PUT' }).then(r => r.json());
  let ws, send, mockScript;
  const errors = [];
  try {
    ws = new WebSocket(tab.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve, { once: true });
      ws.addEventListener('error', reject, { once: true });
    });
    let sequence = 0;
    const pending = new Map();
    ws.addEventListener('message', event => {
      const msg = JSON.parse(event.data);
      if (msg.method === 'Runtime.exceptionThrown') errors.push(msg.params.exceptionDetails);
      const task = pending.get(msg.id);
      if (!task) return;
      pending.delete(msg.id);
      clearTimeout(task.timeout);
      if (msg.error) task.reject(new Error(JSON.stringify(msg.error)));
      else task.resolve(msg.result);
    });
    send = (method, params = {}) => new Promise((resolve, reject) => {
      const id = ++sequence;
      const timeout = setTimeout(() => {
        pending.delete(id);
        reject(new Error('CDP timeout: ' + method));
      }, 15000);
      pending.set(id, { resolve, reject, timeout });
      ws.send(JSON.stringify({ id, method, params }));
    });
    const evaluate = async expression => {
      const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    const until = async expression => {
      for (let i = 0; i < 80; i++) {
        if (await evaluate(expression)) return;
        await delay(75);
      }
      throw new Error('Timed out: ' + expression);
    };
    const settle = () => until('!guidesBusy && !inputBusy && inputQueue.length===0 && activePosts===0');
    const pausePolling = async () => {
      await until('!positionBusy');
      await evaluate('clearTimeout(positionTimer)');
    };
    const resetCalls = async () => { await settle(); await evaluate('testCalls=[]'); };
    const poll = async () => {
      await pausePolling();
      await evaluate('(async()=>{await pollPosition();clearTimeout(positionTimer)})()');
    };
    const gridVisible = () => evaluate("getComputedStyle($('referenceGrid')).display!=='none'");
    const state = () => evaluate("({on:guidesOn,mode:padMode,pressed:$('guidesToggle').getAttribute('aria-pressed'),disabled:$('guidesToggle').disabled,relativeDisabled:$('relativeMode').disabled})");
    const rect = id => evaluate(`(() => {const r=$('${id}').getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};})()`);
    const points = new Map();
    const touch = async (type, id, x, y) => {
      const ending = points.get(id);
      if (type === 'touchCancel') points.clear();
      else if (type === 'touchEnd') points.delete(id);
      else points.set(id, { x, y, radiusX: 3, radiusY: 3, force: 1, id });
      await send('Input.dispatchTouchEvent', {
        type, touchPoints: type === 'touchEnd' ? (ending ? [ending] : []) : Array.from(points.values())
      });
    };
    const screenshot = async filename => {
      const shot = await send('Page.captureScreenshot', { format: 'png' });
      fs.writeFileSync(path.join(__dirname, filename), Buffer.from(shot.data, 'base64'));
    };

    await send('Page.enable');
    await send('Runtime.enable');
    await send('Emulation.setDeviceMetricsOverride', { width: 1920, height: 960, deviceScaleFactor: 1, mobile: false });
    await send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
    mockScript = await send('Page.addScriptToEvaluateOnNewDocument', { source: `
      if(location.origin==='http://127.0.0.1:7050'){
        localStorage.setItem('pad-mode','relative');
        localStorage.setItem('control-overlay','on');
        localStorage.setItem('pointer-preview','on');
        const nativeFetch=window.fetch.bind(window);
        window.testScreen={x:-1920,y:0,width:3840,height:1200};
        window.testCursor={x:0,y:480};
        window.testGuides=false;window.testGuideFailure='';
        window.testCalls=[];window.activePosts=0;window.guideDelay=90;window.stateDelay=0;
        const json=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
        window.fetch=async(url,options={})=>{
          const route=String(url),method=String(options.method||'GET').toUpperCase();
          if(method==='POST'){
            const data=JSON.parse(options.body||'{}');
            testCalls.push({route,data,time:performance.now()});activePosts++;
            try{
              await new Promise(resolve=>setTimeout(resolve,route==='/guides'?guideDelay:25));
              if(route==='/guides'){
                if(testGuideFailure==='http')return json({error:'Simulated native overlay failure'},503);
                if(testGuideFailure==='reported')return json({ok:false,enabled:!testGuides,error:'Simulated renderer failure'});
                if(testGuideFailure==='malformed')return json({ok:true});
                testGuides=data.enabled;return json({ok:true,enabled:testGuides,error:''});
              }
              if(route==='/move'){
                if(data.mode==='absolute'){
                  testCursor.x=testScreen.x+data.nx*(testScreen.width-1);
                  testCursor.y=testScreen.y+data.ny*(testScreen.height-1);
                }else{testCursor.x+=data.dx;testCursor.y+=data.dy;}
              }
              return json({ok:true,...testCursor});
            }finally{activePosts--;}
          }
          if(route==='/state'){
            const snapshot={ok:true,screen:{...testScreen},cursor:{...testCursor},guides:{ok:true,enabled:testGuides,error:''}};
            if(stateDelay)await new Promise(resolve=>setTimeout(resolve,stateDelay));
            return json(snapshot);
          }
          if(route==='/guides')return json({ok:true,enabled:testGuides,error:''});
          return nativeFetch(url,options);
        };
        navigator.sendBeacon=(url,data)=>{testCalls.push({route:String(url),beacon:true});return true;};
      }
    ` });
    await send('Page.navigate', { url: 'http://127.0.0.1:7050/' });
    await send('Page.bringToFront');
    await until("typeof logged!=='undefined' && logged && typeof testCalls!=='undefined' && $('referenceGrid')");
    await pausePolling();

    assert.deepEqual(await state(), { on: false, mode: 'relative', pressed: 'false', disabled: false, relativeDisabled: false });
    assert.equal(await gridVisible(), false);
    assert.equal(await evaluate("document.querySelectorAll('#referenceGrid .vertical').length"), 2);
    assert.equal(await evaluate("document.querySelectorAll('#referenceGrid .horizontal').length"), 2);
    assert.equal(await evaluate("document.querySelectorAll('#referenceGrid .guide-intersection').length"), 4);
    const offPad = await rect('pad');
    console.log('PASS reference lines start off without changing saved relative mode');

    await resetCalls();
    await evaluate("guideDelay=250;$('guidesToggle').click();$('guidesToggle').click();updatePadMode('relative')");
    assert.equal(await evaluate('guidesBusy'), true);
    assert.equal(await evaluate("$('relativeMode').disabled && $('absoluteMode').disabled && $('guidesToggle').disabled"), true);
    assert.equal(await gridVisible(), false, 'no local grid before the teaching PC acknowledges enable');
    await touch('touchStart', 1, offPad.x + offPad.width / 3, offPad.y + offPad.height / 3);
    await touch('touchEnd', 1);
    await settle();
    assert.equal(await evaluate("testCalls.filter(t=>t.route==='/guides').length"), 1, 'double activation must produce one request');
    assert.equal(await evaluate("testCalls.some(t=>t.route==='/move' || t.route==='/button')"), false, 'pad input pauses while mapping is changing');
    assert.equal(await evaluate("testCalls.find(t=>t.route==='/guides').data.enabled"), true);
    assert.deepEqual(await state(), { on: true, mode: 'absolute', pressed: 'true', disabled: false, relativeDisabled: true });
    assert.equal(await gridVisible(), true);
    assert.deepEqual(await rect('pad'), offPad, 'enabling lines must not reserve pad space');
    await evaluate("$('relativeMode').click();updatePadMode('relative');guideDelay=90");
    assert.equal(await evaluate('padMode'), 'absolute', 'both UI and function guard relative mode while guides are on');
    console.log('PASS acknowledged enable, double-click guard, transition input guard and absolute-mode protection');

    await resetCalls();
    await evaluate("$('guidesToggle').click()");
    await settle();
    assert.equal(await gridVisible(), false);
    assert.equal(await evaluate("testCalls.find(t=>t.route==='/guides').data.enabled"), false);
    assert.equal(await evaluate("$('relativeMode').disabled"), false);
    await evaluate("$('relativeMode').click()");
    assert.equal(await evaluate('padMode'), 'relative');
    for (const failure of ['http', 'reported', 'malformed']) {
      await resetCalls();
      await pausePolling();
      await evaluate(`testGuideFailure=${JSON.stringify(failure)};$('guidesToggle').click()`);
      await settle();
      assert.equal(await evaluate('guidesOn'), false, failure + ' must not claim success');
      assert.equal(await gridVisible(), false);
      assert.equal(await evaluate('padMode'), 'relative', failure + ' must preserve original mapping');
      assert.equal(await evaluate("$('guidesToggle').textContent"), '参考线重试');
      assert.equal(await evaluate("$('guidesToggle').disabled"), false);
    }
    await evaluate('testGuideFailure=""');
    console.log('PASS disable restores mode choice; HTTP/native/malformed failures never fake success');

    await resetCalls();
    await evaluate('testGuides=true');
    await poll();
    assert.equal(await gridVisible(), true);
    assert.equal(await evaluate('padMode'), 'absolute');
    await evaluate('testGuides=false');
    await poll();
    assert.equal(await gridVisible(), false);
    assert.equal(await evaluate("testCalls.filter(t=>t.route==='/guides').length"), 0, 'other-client state must not echo a mutation back');
    console.log('PASS server state synchronizes another client enable and disable');

    await pausePolling();
    await evaluate('stateDelay=350;window.oldGuidePoll=pollPosition();true');
    await evaluate("guideDelay=70;$('guidesToggle').click()");
    await settle();
    await until('!positionBusy');
    await evaluate('clearTimeout(positionTimer);stateDelay=0;guideDelay=90');
    assert.equal(await evaluate('guidesOn'), true, 'older off-state poll must not overwrite a newer successful toggle');
    assert.equal(await gridVisible(), true);
    console.log('PASS in-flight old state cannot overwrite the toggle acknowledgement');

    await resetCalls();
    await evaluate('setLogged(false)');
    assert.equal(await gridVisible(), false, 'signed-out client hides stale local reference lines');
    assert.equal(await evaluate("$('guidesToggle').disabled"), true);
    await evaluate("$('guidesToggle').onclick()");
    await settle();
    assert.equal(await evaluate("testCalls.some(t=>t.route==='/guides')"), false);
    await evaluate('setLogged(true)');
    await poll();
    assert.equal(await gridVisible(), true, 'reconnect restores actual teaching PC guide state');
    console.log('PASS login visibility and mutation guards');

    await until('!previewBusy');
    await evaluate("if(overlayOn)$('overlayToggle').click();if(previewOn)$('previewToggle').click()");
    await settle();
    assert.equal(await gridVisible(), true, 'guide switch is independent of auxiliary controls and image preview');
    assert.deepEqual(await rect('pad'), offPad);
    assert.equal(await evaluate("getComputedStyle($('referenceGrid')).pointerEvents"), 'none');

    for (const [width, height, filename] of [[1920, 960, '7050-guides-16x8.png'], [1920, 1200, '7050-guides-16x10.png'], [640, 360, null]]) {
      await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
      await delay(100);
      const pad = await rect('pad');
      assert.ok(pad.width * pad.height / (width * height) > (width < 700 ? .82 : .9), 'reference lines keep the blind pad large');
      assert.equal(await evaluate('document.documentElement.scrollWidth>innerWidth || document.documentElement.scrollHeight>innerHeight'), false);
      const geometry = await evaluate(`(() => {
        const p=$('pad').getBoundingClientRect();
        const center=n=>{const r=n.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,nx:(r.x+r.width/2-p.x)/p.width,ny:(r.y+r.height/2-p.y)/p.height};};
        return {
          vertical:Array.from(document.querySelectorAll('#referenceGrid .vertical'),center),
          horizontal:Array.from(document.querySelectorAll('#referenceGrid .horizontal'),center),
          intersections:Array.from(document.querySelectorAll('#referenceGrid .guide-intersection'),center)
        };
      })()`);
      for (let i = 0; i < 2; i++) {
        assert.ok(Math.abs(geometry.vertical[i].nx - (i + 1) / 3) < .001, 'vertical line normalized position');
        assert.ok(Math.abs(geometry.horizontal[i].ny - (i + 1) / 3) < .001, 'horizontal line normalized position');
      }
      for (let i = 0; i < geometry.intersections.length; i++) {
        const point = geometry.intersections[i];
        const nx = (i % 2 + 1) / 3, ny = (Math.floor(i / 2) + 1) / 3;
        assert.ok(Math.abs(point.nx - nx) < .001 && Math.abs(point.ny - ny) < .001, 'intersections stay at thirds after resize');
        assert.equal(await evaluate(`document.elementFromPoint(${point.x},${point.y})?.closest('#pad')?.id`), 'pad', 'guide intersection cannot intercept the touch');
        await resetCalls();
        await touch('touchStart', 1, point.x, point.y);
        await delay(25);
        await touch('touchEnd', 1);
        await settle();
        const calls = await evaluate('testCalls');
        const move = calls.findIndex(t => t.route === '/move');
        const click = calls.findIndex(t => t.route === '/button' && t.data.action === 'click');
        assert.ok(move >= 0 && click > move, 'touching a guide intersection positions before clicking');
        assert.equal(calls[move].data.mode, 'absolute');
        assert.ok(Math.abs(calls[move].data.nx - nx) < .003 && Math.abs(calls[move].data.ny - ny) < .003, 'touch normalized coordinates match both-screen thirds');
        assert.equal(calls.filter(t => t.route === '/button').length, 1);
      }
      if (filename) await screenshot(filename);
      console.log(`PASS ${width}x${height}: fixed thirds, click-through, absolute touch mapping and large pad`);
    }
    assert.equal(errors.length, 0, JSON.stringify(errors));
    console.log('All shared-reference-grid browser checks passed. No real desktop input or guide POST was sent.');
  } finally {
    if (send && mockScript) await send('Page.removeScriptToEvaluateOnNewDocument', { identifier: mockScript.identifier }).catch(() => {});
    if (send) await send('Page.navigate', { url: 'about:blank' }).catch(() => {});
    if (ws) ws.close();
    await fetch('http://127.0.0.1:9705/json/close/' + tab.id).catch(() => {});
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
