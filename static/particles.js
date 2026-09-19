(function() {
    // Respect prefers-reduced-motion setting
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        return;
    }

    const auroraContainers = document.querySelectorAll('.aurora');
    if (!auroraContainers.length) return;

    auroraContainers.forEach(container => {
        const canvas = document.createElement('canvas');
        canvas.style.position = 'absolute';
        canvas.style.top = '0';
        canvas.style.left = '0';
        canvas.style.width = '100%';
        canvas.style.height = '100%';
        canvas.style.pointerEvents = 'none';
        canvas.style.zIndex = '0';
        container.appendChild(canvas);
        
        const computedStyle = window.getComputedStyle(container);
        if (computedStyle.position === 'static') {
            container.style.position = 'relative';
        }

        const ctx = canvas.getContext('2d');
        let width, height;
        
        function resize() {
            width = container.offsetWidth;
            height = container.offsetHeight;
            canvas.width = width;
            canvas.height = height;
        }
        
        window.addEventListener('resize', resize);
        resize();

        const colors = [
            'rgba(88, 101, 242, 0.3)',
            'rgba(139, 92, 246, 0.2)',
            'rgba(236, 72, 153, 0.15)'
        ];

        const particles = [];
        const numParticles = 40;

        class Particle {
            constructor() {
                this.reset(true);
            }

            reset(initial = false) {
                this.x = Math.random() * width;
                this.y = initial ? Math.random() * height : height + 10;
                this.size = Math.random() * 2 + 1;
                this.speedY = -(Math.random() * 0.5 + 0.1);
                this.speedX = (Math.random() - 0.5) * 0.3;
                this.color = colors[Math.floor(Math.random() * colors.length)];
                this.isPaw = Math.random() > 0.8;
                this.opacity = Math.random() * 0.5 + 0.1;
            }

            update() {
                this.y += this.speedY;
                this.x += this.speedX;
                
                if (this.y < -10 || this.x < -10 || this.x > width + 10) {
                    this.reset();
                }
            }

            draw() {
                ctx.globalAlpha = this.opacity;
                ctx.fillStyle = this.color;
                
                if (this.isPaw) {
                    const s = this.size * 1.5;
                    ctx.beginPath();
                    ctx.arc(this.x, this.y, s, 0, Math.PI * 2);
                    ctx.arc(this.x - s*1.2, this.y - s*1.2, s*0.6, 0, Math.PI * 2);
                    ctx.arc(this.x + s*1.2, this.y - s*1.2, s*0.6, 0, Math.PI * 2);
                    ctx.arc(this.x, this.y - s*1.8, s*0.7, 0, Math.PI * 2);
                    ctx.fill();
                } else {
                    ctx.beginPath();
                    ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
                    ctx.fill();
                }
            }
        }

        for (let i = 0; i < numParticles; i++) {
            particles.push(new Particle());
        }

        function animate() {
            ctx.clearRect(0, 0, width, height);
            
            particles.forEach(p => {
                p.update();
                p.draw();
            });
            
            requestAnimationFrame(animate);
        }

        animate();
    });
})();
