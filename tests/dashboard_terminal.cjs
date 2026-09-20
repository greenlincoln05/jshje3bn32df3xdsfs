const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('src/btcbot/dashboard.html','utf8');
const script=html.split('<script>')[1].split('</script>')[0];
new vm.Script(script); // Compile the entire shipped script, not a duplicate implementation.
const elements={};
const el=id=>elements[id]??=( {value:'',innerHTML:'',textContent:'',style:{},className:''} );
el('book-group').value='0';el('book-levels').value='20';el('impact-size').value='25';
const ctx=vm.createContext({$:el,market:null,side:'yes',Date,Number,Map,Math,performance:{now:()=>0},num:n=>String(Number(n.toFixed(4))),cents:n=>n==null?'--':Number((n*100).toFixed(4))+'¢'});
vm.runInContext(script.slice(script.indexOf('function cleanLevels('),script.indexOf('function updateAge(')),ctx);
vm.runInContext(`market={book:{yes:[['0.99','0'],['0.4','10'],['0.4','5']],no:[['0.55','20'],['0.50','10']]}};renderBook()`,ctx);
assert.equal(el('q-yes').textContent,'BUY YES 45¢');
assert.equal(el('q-no').textContent,'BUY NO 60¢');
assert.equal(el('st-spread').textContent,'5¢');
assert.match(el('bids').innerHTML,/>15<\/span>/);
assert.match(el('impact-result').textContent,/46¢.*11.50/);
el('impact-size').value='31';vm.runInContext('renderBook()',ctx);
assert.match(el('impact-result').textContent,/Only 30 of 31/);
vm.runInContext(`side='no';renderBook()`,ctx);
assert.equal(el('mid-price').textContent,'57.5¢');
el('book-group').value='0.05';
const grouped=vm.runInContext(`ladder([[0.41,10],[0.43,5]],false)`,ctx);
assert.match(grouped,/>6.25<\/span>/); // Actual notional, not rounded bucket price × quantity.
const now=Date.now();ctx.Date={now:()=>now}; // Use a constructor with a fixed now for deterministic countdown.
ctx.Date=class extends Date {static now(){return now}};
vm.runInContext(`market={close_time:new Date(${now+61234}).toISOString()}`,ctx);
assert.equal(vm.runInContext('timeLeft().text',ctx),'01:01.234');
vm.runInContext(`market.close_time=new Date(${now-1}).toISOString()`,ctx);
assert.equal(vm.runInContext('timeLeft().text',ctx),'00:00.000');
assert.equal(vm.runInContext('timeLeft().live',ctx),false);
console.log('Dashboard terminal: script, binary depth, duplicate/zero levels, liquidity, grouped notional, and millisecond countdown checks passed');
