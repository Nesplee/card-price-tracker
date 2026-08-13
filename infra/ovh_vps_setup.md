# Provisioning et durcissement du VPS de déploiement (Mois 1, Task 5)

> Remplace `infra/oracle_vm_setup.md` prévu dans le plan Mois 1 : après 368+ tentatives infructueuses (~22h) pour provisionner une VM Always Free sur Oracle Cloud (capacité indisponible en permanence sur `VM.Standard.A1.Flex`, région eu-zurich-1, la seule home region possible car figée au signup), le choix s'est porté sur un petit VPS payant chez OVH (~10-12€/mois) pour ne pas bloquer davantage le calendrier du projet. Le réseau et la clé SSH générés pour la tentative Oracle (`~/.ssh/card-tracker-vm.pem` / `.pub`) ont été réutilisés tels quels.

## Caractéristiques du serveur

- **Fournisseur / offre** : OVHcloud, VPS-3 (gamme "2027")
- **Ressources** : 6 vCores, 12 Go RAM, 100 Go stockage NVMe
- **Localisation** : Roubaix (EU-WEST-RBX), France — pas de datacenter Suisse disponible dans le configurateur au moment de la commande
- **Engagement** : sans engagement (flexibilité de résiliation à tout moment)
- **OS** : Ubuntu 24.04 LTS
- **Nom d'hôte** : `vps-6a6b48af.vps.ovh.net`
- **IPv4** : `164.132.243.29`
- **Utilisateur système** : `ubuntu` (pas de compte root actif en SSH — voir durcissement ci-dessous)

## Piège rencontré à la première connexion (à savoir pour une future réinstallation)

Sur cette génération de VPS OVH, l'image Ubuntu livrée par défaut a l'authentification SSH par mot de passe désactivée par défaut au niveau du compte (mot de passe verrouillé), donc la connexion par mot de passe échoue systématiquement même avec le bon mot de passe. La bonne méthode :

1. Fournir une clé SSH publique directement dans le formulaire de réinstallation OVH (Console > VPS > Accueil > "..." à côté de "OS / Distribution" > Réinstaller le VPS) plutôt que de compter sur un accès par mot de passe.
2. La toute première connexion SSH par clé fonctionne, mais le compte `ubuntu` a un mot de passe UNIX expiré par défaut ("administrator enforced") : le système bloque tant qu'un changement de mot de passe interactif n'est pas fait. Le mot de passe "actuel" demandé à cette étape n'est ni le mot de passe du compte OVH (dashboard), ni deviné à l'avance — il est révélé via un lien envoyé par email par OVH au moment de la (ré)installation.
3. Une fois ce changement de mot de passe fait une seule fois, les connexions suivantes par clé SSH sont directes, sans aucune invite.

## Durcissement SSH effectué

Fichier créé : `/etc/ssh/sshd_config.d/00-hardening.conf` (préfixe `00-` pour être inclus avant `50-cloud-init.conf` et `60-cloudimg-settings.conf`, qui contiennent des réglages par défaut contradictoires — sshd applique la première valeur rencontrée pour chaque option) :

```
PasswordAuthentication no
PermitRootLogin no
```

Vérifié avec `sudo sshd -T -C user=ubuntu,host=<IP>,addr=<IP>` avant et après `sudo systemctl restart ssh`, et accès par clé re-testé après redémarrage avant de considérer l'étape terminée (pour ne pas risquer un verrouillage total de l'accès).

## Firewall (UFW)

Seul le port SSH est ouvert pour l'instant :

```bash
sudo ufw allow OpenSSH
sudo ufw enable
```

```
Status: active
Default: deny (incoming), allow (outgoing), disabled (routed)
22/tcp (OpenSSH)      ALLOW IN    Anywhere
22/tcp (OpenSSH (v6)) ALLOW IN    Anywhere (v6)
```

**Mise à jour (Mois 3)** : décision finalement prise de ne JAMAIS ouvrir les ports Postgres (5432) ni Airflow (8080) dans UFW, même après déploiement — voir la section Déploiement ci-dessous. Ces services restent liés à `127.0.0.1` sur le VPS (jamais sur l'interface publique) et accessibles uniquement via tunnel SSH. Le firewall reste donc identique à l'état de fin de Mois 1 (seul le port 22 exposé), pour toute la durée du projet.

## Docker

Installé via le script officiel :

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
```

Vérifié avec `docker compose version` (v5.4.0) et `docker run --rm hello-world` (succès, sans avoir besoin de `sudo` grâce à l'appartenance au groupe `docker`).

## État à la fin de cette tâche

VPS actif, joignable en SSH par clé uniquement, durci (pas de mot de passe, pas de root SSH, firewall restreint à SSH), Docker + Docker Compose opérationnels. Prêt à servir de cible de déploiement au Mois 3 — aucun service applicatif (Postgres, Airflow) n'est encore installé dessus, conformément au séquencement décidé lors du brainstorming initial (infra provisionnée tôt, déploiement réel repoussé au Mois 3 une fois le pipeline stabilisé en local).

---

## Déploiement (Mois 3)

### Alias SSH pratique

Ajouté dans `~/.ssh/config` (machine de développement), pour éviter de retaper l'IP/utilisateur/clé à chaque connexion :

```
Host card-tracker-vm
    HostName 164.132.243.29
    User ubuntu
    IdentityFile ~/.ssh/card-tracker-vm.pem
```

Toutes les commandes ci-dessous supposent cet alias.

### Premier déploiement

```bash
ssh card-tracker-vm
git clone <url-du-repo> card-price-tracker
cd card-price-tracker
cp .env.example .env   # remplir toutes les valeurs (jamais commité)
```

Démarrage de la stack complète (Postgres applicatif, Postgres métadonnées Airflow, Airflow webserver + scheduler, Metabase) :

```bash
docker compose -f docker-compose.prod.yml up -d
./scripts/apply_migrations.sh docker-compose.prod.yml
```

### Mises à jour ultérieures

Le code Python (`src/`, `dags/`) est monté en volume dans les conteneurs Airflow déjà en cours d'exécution — un simple `git pull` suffit, **pas besoin de rebuild ni de redémarrage** pour que le nouveau code soit pris en compte au prochain déclenchement du DAG :

```bash
ssh card-tracker-vm
cd card-price-tracker
git pull
```

Si une migration SQL a été ajoutée : `./scripts/apply_migrations.sh docker-compose.prod.yml`.
Si `docker-compose.prod.yml` a changé (nouveau service, image mise à jour) : `docker compose -f docker-compose.prod.yml up -d`.

### Accéder aux interfaces web (Airflow, Metabase)

Aucun des deux n'est exposé publiquement (voir Firewall ci-dessus) — accès uniquement via tunnel SSH, un port local par service :

```bash
ssh -L 8080:localhost:8080 card-tracker-vm   # UI Airflow -> http://localhost:8080
ssh -L 3000:localhost:3000 card-tracker-vm   # UI Metabase -> http://localhost:3000
```

Si le port local choisi est déjà occupé par autre chose sur ta machine, changer uniquement le premier nombre (ex: `-L 3001:localhost:3000`) — le port distant (après le `:`) ne doit jamais changer.

### Vérifier l'état de la stack

```bash
docker compose -f docker-compose.prod.yml ps
```

Tous les services persistants (`db`, `airflow-db`, `airflow-webserver`, `airflow-scheduler`, `metabase`) doivent afficher `Up ... (healthy)`. `airflow-init` est un conteneur à usage unique (migration + création du compte admin) : il apparaît `Exited (0)` après son passage, ce qui est l'état normal, pas une erreur.
