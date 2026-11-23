(() => {
  const price=document.getElementById('detail-price');
  price.textContent=new Intl.NumberFormat('ru-RU').format(Number(price.dataset.price))+' ₽';
  const image=document.getElementById('detail-photo'); if(!image)return;
  const error=document.getElementById('detail-photo-error');
  let timer=null;
  const failed=()=>{clearTimeout(timer); image.hidden=true; error.hidden=false;};
  const loaded=()=>{clearTimeout(timer); image.hidden=false; error.hidden=true;};
  const choose=url=>{clearTimeout(timer); image.hidden=false; error.hidden=true; timer=setTimeout(failed,10000); image.src=url;};
  image.addEventListener('error',failed);
  image.addEventListener('load',loaded);
  if(image.complete){if(image.naturalWidth)loaded();else failed();}
  else timer=setTimeout(failed,10000);
  document.querySelectorAll('[data-photo]').forEach(button=>button.addEventListener('click',()=>{
    choose(button.dataset.photo);
    document.querySelectorAll('[data-photo]').forEach(n=>n.classList.toggle('active',n===button));
  }));
})();
