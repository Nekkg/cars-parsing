(() => {
  const price=document.getElementById('detail-price');
  price.textContent=new Intl.NumberFormat('ru-RU').format(Number(price.dataset.price))+' ₽';
  const image=document.getElementById('detail-photo'); if(!image)return;
  const failed=()=>{document.getElementById('detail-photo-error').hidden=false;};
  image.addEventListener('error',failed);
  if(image.complete && image.naturalWidth===0)failed();
  document.querySelectorAll('[data-photo]').forEach(button=>button.addEventListener('click',()=>{
    image.src=button.dataset.photo; document.getElementById('detail-photo-error').hidden=true;
    document.querySelectorAll('[data-photo]').forEach(n=>n.classList.toggle('active',n===button));
  }));
})();
