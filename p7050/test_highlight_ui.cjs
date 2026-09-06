// Isolated Edge CDP regression tests. All POSTs and beacons are intercepted.
const assert = require('node:assert/strict');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  const tab = await fetch('http://127.0.0.1:9705/json/new?about:blank', { method: 'PUT' }).then(r => r.json());
  const ws = new WebSocket(tab.webSocketDebuggerUrl);
  const pending = new Map(), errors = [];
  let sequence = 0, send;
  try {
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve, { once: true });
      ws.addEventListener('error', reject, { once: true });
    });
    ws.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
      const task = pending.get(message.id);
      if (!task) return;
      pending.delete(message.id);
      clearTimeout(task.timer);
      if (message.error) task.reject(new Error(JSON.stringify(message.error)));
      else task.resolve(message.result);
    });
    send = (method, params = {}) => new Promise((resolve, reject) => {
      const id = ++sequence;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error('CDP timeout: ' + method));
      }, 15000);
      pending.set(id, { resolve, reject, timer });
      ws.send(JSON.stringify({ id, method, params }));
    });
    const evaluate = async expression => {
      const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    const until = async expression => {
      for (let i = 0; i < 100; i++) {
        if (await evaluate(expression)) return;
        await sleep(50);
      }
      throw new Error('Timed out: ' + expression);
    };
    const settle = () => until('!highlightBusy && !guidesBusy && activePosts===0');
    const pausePolling = async () => {
      await until('!positionBusy');
      await evaluate('clearTimeout(positionTimer)');
    };
    const poll = async () => {
      await pausePolling();
      await evaluate('(async()=>{await pollPosition();clearTimeout(positionTimer)})()');
    };
    const state = () => evaluate("({on:highlightOn,pressed:$('highlightToggle').getAttribute('aria-pressed'),disabled:$('highlightToggle').disabled,mode:padMode,guides:guidesOn})");
    const rectangle = id => evaluate(`(() => {const r=$('${id}').getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};})()`);
    const resetCalls = async () => { await settle(); await evaluate('testCalls=[]'); };

    await send('Page.enable');
    await send('Runtime.enable');
    await send('Emulation.setDeviceMetricsOverride', { width: 1920, height: 960, deviceScaleFactor: 1, mobile: false });
    await send('Page.addScriptToEvaluateOnNewDocument', { source: `
      if(location.origin==='http://127.0.0.1:7050'){
        localStorage.setItem('pad-mode','relative');
        localStorage.setItem('pointer-preview','off');
        localStorage.setItem('control-overlay','off');
        const nativeFetch=window.fetch.bind(window);
        const json=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
        window.testHighlight=false;window.testGuides=false;window.testFailure='';
        window.highlightDelay=90;window.stateDelay=0;window.activePosts=0;window.testCalls=[];
        window.testScreen={x:0,y:0,width:1920,height:1080};
        window.fetch=async(url,options={})=>{
          const route=String(url),method=String(options.method||'GET').toUpperCase();
          if(method==='POST'){
            const data=JSON.parse(options.body||'{}');
            testCalls.push({route,data});activePosts++;
            try{
              await new Promise(resolve=>setTimeout(resolve,route==='/crosshair'?highlightDelay:20));
              if(route==='/crosshair'){
                if(testFailure==='http')return json({error:'Simulated transport failure'},503);
                if(testFailure==='reported')return json({ok:false,enabled:!testHighlight,error:'Simulated renderer failure'});
                if(testFailure==='malformed')return json({ok:true});
                if(testFailure==='unauthorized')return json({error:'Sign in required'},401);
                testHighlight=data.enabled;return json({ok:true,enabled:testHighlight,error:''});
              }
              if(route==='/guides'){testGuides=data.enabled;return json({ok:true,enabled:testGuides,error:''});}
              return json({ok:true,x:960,y:540});
            }finally{activePosts--;}
          }
          if(route==='/state'){
            const snapshot={ok:true,screen:{...testScreen},cursor:{x:960,y:540},highlight:{ok:true,enabled:testHighlight,error:''},guides:{ok:true,enabled:testGuides,error:''}};
            if(stateDelay)await new Promise(resolve=>setTimeout(resolve,stateDelay));
            return json(snapshot);
          }
          if(route==='/crosshair')return json({ok:true,enabled:testHighlight,error:''});
          if(route==='/guides')return json({ok:true,enabled:testGuides,error:''});
          if(route==='/pointer-view')return json({error:'Preview intentionally mocked'},503);
          return nativeFetch(url,options);
        };
        navigator.sendBeacon=(url,data)=>{testCalls.push({route:String(url),beacon:true});return true;};
      }
    ` });
    await send('Page.navigate', { url: 'http://127.0.0.1:7050/' });
    await send('Page.bringToFront');
    await until("typeof highlightOn!=='undefined' && logged && $('highlightToggle') && typeof testCalls!=='undefined'");
    await pausePolling();
    const initialPad = await rectangle('pad');
    assert.deepEqual(await state(), { on: false, pressed: 'false', disabled: false, mode: 'relative', guides: false });
    console.log('PASS explicit mouse-highlight control starts off in saved relative mode');

    await resetCalls();
    await evaluate("highlightDelay=250;$('highlightToggle').click();$('highlightToggle').click()");
    assert.equal(await evaluate('highlightBusy'), true);
    assert.equal(await evaluate("$('highlightToggle').disabled"), true);
    assert.equal(await evaluate('highlightOn'), false, 'must await native renderer acknowledgement');
    await settle();
    assert.deepEqual(await evaluate("testCalls.filter(c=>c.route==='/crosshair').map(c=>c.data)"), [{ enabled: true }]);
    assert.deepEqual(await state(), { on: true, pressed: 'true', disabled: false, mode: 'relative', guides: false });
    assert.deepEqual(await rectangle('pad'), initialPad);
    await evaluate("highlightDelay=90;$('highlightToggle').click()");
    await settle();
    assert.deepEqual(await evaluate("testCalls.filter(c=>c.route==='/crosshair').map(c=>c.data)"), [{ enabled: true }, { enabled: false }]);
    assert.equal(await evaluate('highlightOn'), false);
    console.log('PASS off/on/off, acknowledgement, double-click guard and unchanged blind-pad area');

    for (const failure of ['http', 'reported', 'malformed']) {
      await pausePolling();
      await resetCalls();
      await evaluate(`testFailure=${JSON.stringify(failure)};$('highlightToggle').click()`);
      await settle();
      assert.equal(await evaluate('highlightOn'), false, failure + ' must not report fake success');
      assert.equal(await evaluate("$('highlightToggle').getAttribute('aria-pressed')"), 'false');
      assert.equal(await evaluate("$('highlightToggle').disabled"), false);
      assert.equal(await evaluate('padMode'), 'relative');
    }
    await evaluate('testFailure=""');
    console.log('PASS transport, native-renderer and malformed failures retain the last confirmed state');

    await resetCalls();
    await evaluate('testHighlight=true');
    await poll();
    assert.equal(await evaluate('highlightOn'), true);
    await evaluate('testHighlight=false');
    await poll();
    assert.equal(await evaluate('highlightOn'), false);
    assert.equal(await evaluate("testCalls.some(c=>c.route==='/crosshair')"), false, 'polling is read-only');
    await evaluate('stateDelay=350;window.pendingOldHighlightPoll=pollPosition();true');
    await evaluate("highlightDelay=65;$('highlightToggle').click()");
    await settle();
    await until('!positionBusy');
    await evaluate('clearTimeout(positionTimer);stateDelay=0;highlightDelay=90');
    assert.equal(await evaluate('highlightOn'), true, 'old off-state poll cannot undo a newer toggle');
    console.log('PASS other-client state synchronization and old-response race protection');

    await resetCalls();
    await evaluate("$('overlayToggle').click();$('previewToggle').click();$('guidesToggle').click()");
    await settle();
    assert.equal(await evaluate('highlightOn && guidesOn && overlayOn && previewOn'), true);
    await evaluate("$('highlightToggle').click()");
    await settle();
    assert.equal(await evaluate('!highlightOn && guidesOn && overlayOn && previewOn'), true);
    await evaluate("$('highlightToggle').click();$('guidesToggle').click()");
    await settle();
    await evaluate("updatePadMode('relative');$('overlayToggle').click();$('previewToggle').click()");
    await settle();
    assert.equal(await evaluate('highlightOn && !guidesOn && !overlayOn && !previewOn && padMode==="relative"'), true);
    assert.deepEqual(await rectangle('pad'), initialPad);
    console.log('PASS independent of reference lines, preview, auxiliary controls and relative mapping');

    await resetCalls();
    await evaluate('setLogged(false)');
    assert.equal(await evaluate("$('highlightToggle').disabled"), true);
    await evaluate("$('highlightToggle').onclick()");
    await settle();
    assert.equal(await evaluate("testCalls.some(c=>c.route==='/crosshair')"), false, 'signed-out handler must not mutate');
    await evaluate('testHighlight=false;setLogged(true)');
    await poll();
    assert.equal(await evaluate('highlightOn'), false, 'reconnect reflects the actual host state');
    await pausePolling();
    await evaluate("testFailure='unauthorized';$('highlightToggle').click()");
    await settle();
    assert.equal(await evaluate('logged'), false);
    assert.equal(await evaluate("$('highlightToggle').disabled"), true);
    await evaluate('testFailure="";setLogged(true)');
    await poll();
    console.log('PASS login/401 mutation guards and reconnect synchronization');

    for (const [width, height] of [[1920, 960], [1920, 1200], [640, 360]]) {
      await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
      await sleep(100);
      await pausePolling();
      const pad = await rectangle('pad');
      assert.ok(pad.width * pad.height / (width * height) > (width < 700 ? .82 : .9));
      assert.equal(await evaluate('document.documentElement.scrollWidth>innerWidth || document.documentElement.scrollHeight>innerHeight'), false);
      const button = await rectangle('highlightToggle');
      assert.ok(button.x >= 0 && button.y >= 0 && button.x + button.width <= width && button.y + button.height <= height);
      assert.ok(button.width >= 44 && button.height >= 32, 'button retains a usable hit area');
      assert.equal(await evaluate(`document.elementFromPoint(${button.x + button.width / 2},${button.y + button.height / 2})?.closest('button')?.id`), 'highlightToggle');
      const before = await evaluate('highlightOn');
      await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: button.x + button.width / 2, y: button.y + button.height / 2, button: 'left', clickCount: 1 });
      await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: button.x + button.width / 2, y: button.y + button.height / 2, button: 'left', clickCount: 1 });
      await settle();
      assert.equal(await evaluate('highlightOn'), !before, 'actual browser hit-test activates the button');
      assert.deepEqual(await rectangle('pad'), pad, 'toggle must not take away touchpad space');
      console.log(`PASS ${width}x${height}: reachable highlight control, no overflow and large unchanged pad`);
    }
    assert.equal(errors.length, 0, JSON.stringify(errors));
    console.log('All mouse-highlight UI checks passed. No real desktop input or overlay POST was sent.');
  } finally {
    if (send) await send('Page.navigate', { url: 'about:blank' }).catch(() => {});
    ws.close();
    for (const task of pending.values()) clearTimeout(task.timer);
    await fetch('http://127.0.0.1:9705/json/close/' + tab.id).catch(() => {});
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
