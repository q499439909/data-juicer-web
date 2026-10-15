(function(root,factory){const renderer=factory();if(typeof module!=="undefined"&&module.exports)module.exports=renderer;if(root)root.renderMarkdown=renderer.renderMarkdown})(typeof globalThis!=="undefined"?globalThis:this,function(){
  const escape=value=>String(value??"").replace(/[&<>'"]/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
  const safeUrl=value=>{const url=String(value||"").trim();return /^(https?:\/\/|\/)/i.test(url)?escape(url):null};
  function inline(value){
    const code=[];
    let text=escape(value).replace(/`([^`]+)`/g,(_,body)=>{code.push(`<code>${body}</code>`);return `\u0000CODE${code.length-1}\u0000`});
    text=text.replace(/\[([^\]]+)\]\(([^)]+)\)/g,(_,label,url)=>{const href=safeUrl(url);return href?`<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`:`${label} (${escape(url)})`});
    text=text.replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>").replace(/__([^_]+)__/g,"<strong>$1</strong>");
    text=text.replace(/(^|[^*])\*([^*\n]+)\*/g,"$1<em>$2</em>");
    return text.replace(/\u0000CODE(\d+)\u0000/g,(_,index)=>code[Number(index)]||"");
  }
  function renderMarkdown(value){
    const lines=String(value??"").replace(/\r\n?/g,"\n").split("\n"),html=[];
    let index=0;
    const special=line=>/^\s*```/.test(line)||/^\s{0,3}#{1,6}\s+/.test(line)||/^\s*([-+*]|\d+\.)\s+/.test(line)||/^\s*>\s?/.test(line);
    while(index<lines.length){
      const line=lines[index];
      if(!line.trim()){index+=1;continue}
      const fence=line.match(/^\s*```([\w-]*)\s*$/);
      if(fence){const body=[];index+=1;while(index<lines.length&&!/^\s*```/.test(lines[index]))body.push(lines[index++]);if(index<lines.length)index+=1;const language=fence[1]?` class="language-${escape(fence[1])}"`:"";html.push(`<pre><code${language}>${escape(body.join("\n"))}</code></pre>`);continue}
      const heading=line.match(/^\s{0,3}(#{1,6})\s+(.+)$/);
      if(heading){const level=heading[1].length;html.push(`<h${level}>${inline(heading[2])}</h${level}>`);index+=1;continue}
      const list=line.match(/^\s*([-+*]|\d+\.)\s+(.+)$/);
      if(list){const ordered=/\d+\./.test(list[1]),tag=ordered?"ol":"ul",items=[];while(index<lines.length){const match=lines[index].match(/^\s*([-+*]|\d+\.)\s+(.+)$/);if(!match||(/\d+\./.test(match[1]))!==ordered)break;items.push(`<li>${inline(match[2])}</li>`);index+=1}html.push(`<${tag}>${items.join("")}</${tag}>`);continue}
      if(/^\s*>\s?/.test(line)){const quote=[];while(index<lines.length&&/^\s*>\s?/.test(lines[index]))quote.push(lines[index++].replace(/^\s*>\s?/,""));html.push(`<blockquote>${quote.map(inline).join("<br>")}</blockquote>`);continue}
      const paragraph=[];while(index<lines.length&&lines[index].trim()&&!special(lines[index]))paragraph.push(lines[index++]);html.push(`<p>${paragraph.map(inline).join("<br>")}</p>`);
    }
    return html.join("");
  }
  return{renderMarkdown};
});
