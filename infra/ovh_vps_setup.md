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

Les ports Postgres (5432) et Airflow (8080) seront ouverts explicitement au Mois 3, au moment du déploiement réel — pas avant, pour limiter la surface d'attaque tant que rien n'est encore déployé dessus.

## Docker

Installé via le script officiel :

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
```

Vérifié avec `docker compose version` (v5.4.0) et `docker run --rm hello-world` (succès, sans avoir besoin de `sudo` grâce à l'appartenance au groupe `docker`).

## État à la fin de cette tâche

VPS actif, joignable en SSH par clé uniquement, durci (pas de mot de passe, pas de root SSH, firewall restreint à SSH), Docker + Docker Compose opérationnels. Prêt à servir de cible de déploiement au Mois 3 — aucun service applicatif (Postgres, Airflow) n'est encore installé dessus, conformément au séquencement décidé lors du brainstorming initial (infra provisionnée tôt, déploiement réel repoussé au Mois 3 une fois le pipeline stabilisé en local).
